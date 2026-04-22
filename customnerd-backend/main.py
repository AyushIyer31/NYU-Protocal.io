from fastapi import FastAPI, BackgroundTasks, HTTPException, UploadFile, Query, Form, File
from fastapi.responses import Response
from starlette.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from sse_starlette.sse import EventSourceResponse
from pydantic import BaseModel
from typing import List, Dict, Any, Optional
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

import asyncio
import uuid
import json
import logging
import os
import shutil
import time
from pathlib import Path

from helper_functions import (
    extract_text_from_upload,
    build_local_rag_index,
    retrieve_relevant_chunks,
    analyze_target_with_ollama,
    ensure_session_dirs,
    check_ollama_health,
    get_default_execution_strategy,
    normalize_execution_strategy,
)

logging.basicConfig(level=logging.INFO)

app = FastAPI()
update_queues: Dict[str, asyncio.Queue] = {}
main_loop: Optional[asyncio.AbstractEventLoop] = None
executor = ThreadPoolExecutor()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_STORAGE_DIR = Path("storage")
SESSIONS_DIR = BASE_STORAGE_DIR / "sessions"


class LocalRAGAnalysisResponse(BaseModel):
    session_id: str


@app.on_event("startup")
async def startup_event():
    global main_loop
    main_loop = asyncio.get_event_loop()
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    logging.info("Local Ollama RAG backend started.")


@app.get("/")
async def root():
    return {
        "message": "Local Ollama RAG backend is running.",
        "routes": [
            "/health",
            "/sse",
            "/process_local_rag_analysis",
        ],
    }


@app.get("/health")
def health_check():
    ollama_status = check_ollama_health()
    storage_ok = SESSIONS_DIR.exists()
    status = "healthy" if ollama_status.get("ok") and storage_ok else "degraded"

    return {
        "status": status,
        "ollama": ollama_status,
        "storage_dir": str(SESSIONS_DIR),
        "storage_ready": storage_ok,
        "message": "Application is running"
    }


@app.get("/sse")
async def sse(session_id: str = Query(default=None)):
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id is required")
    return EventSourceResponse(event_generator(session_id))


async def event_generator(session_id: str):
    # Reuse the queue created by the POST endpoint, or create one if SSE connects first.
    if session_id not in update_queues:
        update_queues[session_id] = asyncio.Queue()
    queue = update_queues[session_id]
    try:
        while True:
            data = await queue.get()
            if isinstance(data, dict) and "final_output" in data:
                yield {"event": "message", "data": json.dumps(data)}
                break
            elif isinstance(data, dict) and "error" in data:
                yield {"event": "message", "data": json.dumps(data)}
                break
            else:
                yield {"event": "message", "data": json.dumps({"update": data})}
    finally:
        update_queues.pop(session_id, None)


def _thread_safe_send_update(session_id: str, message: Any):
    """
    Send an update from a background thread to the SSE queue on the main event loop.
    Thread-safe: uses call_soon_threadsafe to schedule the put on the correct loop.
    """
    queue = update_queues.get(session_id)
    if queue and main_loop:
        main_loop.call_soon_threadsafe(queue.put_nowait, message)


def save_uploaded_file(file_info: Dict[str, Any], destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with open(destination, "wb") as f:
        f.write(file_info["content"])
    return destination


@app.post("/process_local_rag_analysis", response_model=LocalRAGAnalysisResponse)
async def process_local_rag_analysis(
    background_tasks: BackgroundTasks,
    user_query: str = Form(...),
    context_files: List[UploadFile] = File(...),
    target_file: UploadFile = File(...),
    analysis_mode: str = Form("compliance"),
    execution_strategy: str = Form(get_default_execution_strategy()),
    top_k: int = Form(8),
    chunk_size: int = Form(1200),
    chunk_overlap: int = Form(200),
):
    if not context_files:
        raise HTTPException(status_code=400, detail="At least one context file is required.")

    unique_id = str(uuid.uuid4())
    update_queues[unique_id] = asyncio.Queue()

    context_file_metadata = []
    for file in context_files:
        file_content = await file.read()
        context_file_metadata.append({
            "filename": file.filename,
            "content_type": file.content_type,
            "content": file_content,
        })

    target_content = await target_file.read()
    target_file_metadata = {
        "filename": target_file.filename,
        "content_type": target_file.content_type,
        "content": target_content,
    }

    background_tasks.add_task(
        process_local_rag_logic,
        user_query,
        context_file_metadata,
        target_file_metadata,
        {
            "analysis_mode": analysis_mode,
            "execution_strategy": normalize_execution_strategy(execution_strategy),
            "top_k": top_k,
            "chunk_size": chunk_size,
            "chunk_overlap": chunk_overlap,
        },
        unique_id,
    )

    return JSONResponse({"session_id": unique_id})


def process_local_rag_logic(
    user_query: str,
    context_file_metadata: List[Dict[str, Any]],
    target_file_metadata: Dict[str, Any],
    options: Dict[str, Any],
    session_id: str,
):
    start_time = time.time()

    try:
        _thread_safe_send_update(session_id, "Preparing local workspace...")
        session_dirs = ensure_session_dirs(SESSIONS_DIR, session_id)

        context_paths = []
        for file_info in context_file_metadata:
            destination = session_dirs["context"] / file_info["filename"]
            context_paths.append(save_uploaded_file(file_info, destination))

        target_path = save_uploaded_file(
            target_file_metadata,
            session_dirs["target"] / target_file_metadata["filename"]
        )

        _thread_safe_send_update(session_id, "Extracting text from context documents...")
        context_documents = []
        for path in context_paths:
            extracted = extract_text_from_upload(path)
            context_documents.append({
                "source_file": path.name,
                "path": str(path),
                "text": extracted,
            })

        _thread_safe_send_update(session_id, "Extracting text from target document...")
        target_text = extract_text_from_upload(target_path)

        if not target_text or not target_text.strip():
            raise ValueError("Target document text extraction failed or returned empty text.")

        _thread_safe_send_update(session_id, "Building local retrieval index...")
        rag_index = build_local_rag_index(
            context_documents=context_documents,
            index_dir=session_dirs["index"],
            chunk_size=options.get("chunk_size", 1200),
            chunk_overlap=options.get("chunk_overlap", 200),
        )

        _thread_safe_send_update(session_id, "Retrieving relevant context sections...")
        retrieved_chunks = retrieve_relevant_chunks(
            rag_index=rag_index,
            user_query=user_query,
            target_text=target_text,
            top_k=options.get("top_k", 8),
        )

        execution_strategy = normalize_execution_strategy(options.get("execution_strategy"))
        if execution_strategy == "prompt_based":
            _thread_safe_send_update(session_id, "Starting prompt-based analysis...")
        else:
            _thread_safe_send_update(session_id, "Starting agentic analysis pipeline...")
        final_output = analyze_target_with_ollama(
            user_query=user_query,
            target_text=target_text,
            retrieved_chunks=retrieved_chunks,
            analysis_mode=options.get("analysis_mode", "compliance"),
            execution_strategy=execution_strategy,
            on_progress=lambda msg: _thread_safe_send_update(session_id, msg),
        )

        runtime = round(time.time() - start_time, 2)

        result = {
            "session_id": session_id,
            "analysis_mode": options.get("analysis_mode", "compliance"),
            "execution_strategy": execution_strategy,
            "target_file": target_file_metadata["filename"],
            "context_files": [x["filename"] for x in context_file_metadata],
            "retrieved_chunk_count": len(retrieved_chunks),
            "runtime_seconds": runtime,
            "final_output": final_output,
        }

        output_path = session_dirs["outputs"] / "result.json"
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)

        _thread_safe_send_update(session_id, {"final_output": result})

    except Exception as e:
        logging.exception("Local RAG pipeline failed")
        _thread_safe_send_update(session_id, {"error": str(e)})


@app.get("/fetch_backend_mode")
async def fetch_backend_mode():
    return {
        "mode": "local_ollama_rag",
        "default_execution_strategy": get_default_execution_strategy(),
        "available_execution_strategies": ["agentic", "prompt_based"],
        "external_search_enabled": False,
        "providers": ["ollama"],
        "data_leaves_machine": False,
    }


@app.get("/ollama_status")
async def ollama_status():
    return check_ollama_health()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
