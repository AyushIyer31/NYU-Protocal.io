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
from protocol_rag import (
    build_protocol_index, search_protocols, explain_matches, classify_intent,
    is_vague_query, get_clarification_question, expand_query, multi_search_protocols,
)
from claude_client import is_available as claude_is_available
from concept_expansion import (
    extract_concepts, expand_concepts, build_search_probes, generate_sentence_variants,
)
from protocolsio_client import multi_probe_search
from protocol_ranker import rank_protocols

logging.basicConfig(level=logging.INFO)

app = FastAPI()
update_queues: Dict[str, asyncio.Queue] = {}
main_loop: Optional[asyncio.AbstractEventLoop] = None
executor = ThreadPoolExecutor()

# Protocol RAG index — built once at startup
PROTOCOL_INDEX: Optional[Dict[str, Any]] = None
PROTOCOLS_DATA_DIR = Path("../data/protocols")

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
    global main_loop, PROTOCOL_INDEX
    main_loop = asyncio.get_event_loop()
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

    if PROTOCOLS_DATA_DIR.exists():
        try:
            loop = asyncio.get_event_loop()
            PROTOCOL_INDEX = await loop.run_in_executor(
                executor, build_protocol_index, PROTOCOLS_DATA_DIR
            )
            count = len(PROTOCOL_INDEX["protocols"])
            logging.info(f"Protocol index ready: {count} protocols loaded.")
        except Exception as e:
            logging.warning(f"Could not build protocol index: {e}")
    else:
        logging.warning(f"Protocol data dir not found: {PROTOCOLS_DATA_DIR}. Run fetch_protocols.py first.")

    logging.info("Local Ollama RAG backend started.")


class ChatRequest(BaseModel):
    query: str
    top_k: int = 5
    explain: bool = True
    # Set True when the user is responding to a clarification question,
    # so we don't ask for clarification a second time.
    skip_clarification: bool = False
    # "live"  -> concept-expansion pipeline against the live protocols.io API
    # "local" -> legacy TF-IDF search over the cached protocol index
    search_mode: str = "live"


def run_live_expansion_search(query: str, top_k: int) -> Dict[str, Any]:
    """
    Concept-expansion search against the live protocols.io API.

    extract concepts -> expand each with grounded synonyms (NCBI/Europe PMC,
    Ollama/static fallback) -> fire short probes -> merge -> multi-signal re-rank.
    Returns results plus the intermediate concepts/expansions/probes for display.
    """
    concepts = extract_concepts(query)
    expansions = expand_concepts(concepts, use_external=True)
    probes = build_search_probes(concepts, expansions, max_probes=10)
    sentence_variants = generate_sentence_variants(concepts, expansions)
    merged, hit_map, probe_totals = multi_probe_search(probes, per_probe=6, cap=50)
    ranked = rank_protocols(concepts, expansions, merged, hit_map, top_k=top_k)
    return {
        "results": ranked,
        "concepts": concepts,
        "expansions": expansions,
        "probes": probes,
        "probe_totals": probe_totals,
        "sentence_variants": sentence_variants,
    }


@app.post("/chat")
async def chat(req: ChatRequest):
    """
    Main chatbot endpoint with clarification, multi-query expansion, and re-ranking.

    Flow:
      1. Classify intent (chitchat vs. protocol search).
      2. If the query is vague and clarification hasn't been skipped, ask a follow-up.
      3. Expand the query into 3-5 related variants.
      4. Run TF-IDF search on all variants, merge and re-rank results.
      5. Optionally generate a plain-English explanation via the local LLM.
      6. Return results with a feedback prompt.
    """
    # Live mode searches protocols.io directly and does not need the local index;
    # only the legacy TF-IDF ("local") mode requires it.
    if req.search_mode == "local" and not PROTOCOL_INDEX:
        raise HTTPException(status_code=503, detail="Protocol index not loaded. Run fetch_protocols.py first.")

    if not req.query or not req.query.strip():
        raise HTTPException(status_code=400, detail="Query cannot be empty.")

    query = req.query.strip()
    loop = asyncio.get_event_loop()
    total_indexed = len(PROTOCOL_INDEX["protocols"]) if PROTOCOL_INDEX else 0

    # Step 1: Check for vague queries BEFORE intent classification.
    # Vague lab terms (e.g. "overexpression", "PCR") may not contain the keywords
    # that the chitchat classifier uses, so they'd be wrongly dismissed as chitchat.
    # Catching them here means they always get a clarification question instead.
    if not req.skip_clarification and is_vague_query(query):
        clarification = get_clarification_question(query)
        return {
            "query": query,
            "intent": "clarification",
            "reply": clarification,
            "results": [],
            "explanation": "",
            "expanded_queries": [],
            "feedback_prompt": None,
            "feedback_options": [],
            "total_protocols_indexed": total_indexed,
        }

    # Step 2: Classify intent — chitchat vs. protocol search
    intent = await loop.run_in_executor(executor, classify_intent, query)
    if intent["intent"] == "chitchat":
        return {
            "query": query,
            "intent": "chitchat",
            "reply": intent["reply"],
            "results": [],
            "explanation": "",
            "expanded_queries": [],
            "feedback_prompt": None,
            "feedback_options": [],
            "total_protocols_indexed": total_indexed,
        }

    # Steps 3-4: search. Either the live concept-expansion pipeline (default) or
    # the legacy local TF-IDF pipeline.
    concepts: Dict[str, Any] = {}
    expansions: Dict[str, List[str]] = {}
    sentence_variants: List[str] = []
    if req.search_mode == "live":
        try:
            live = await loop.run_in_executor(executor, run_live_expansion_search, query, req.top_k)
            results = live["results"]
            concepts = live["concepts"]
            expansions = live["expansions"]
            expanded = live["probes"]
            sentence_variants = live["sentence_variants"]
            logging.info(f"Live search '{query}': {len(expanded)} probes -> {len(results)} ranked results")
        except Exception as e:
            logging.warning(f"Live search failed ({e}); falling back to local index.")
            if not PROTOCOL_INDEX:
                raise HTTPException(status_code=503, detail=f"Live search failed and no local index: {e}")
            expanded = await loop.run_in_executor(executor, expand_query, query)
            results = await loop.run_in_executor(
                executor, multi_search_protocols, PROTOCOL_INDEX, expanded, req.top_k
            )
    else:
        # Step 3: Expand the query into multiple related search variants
        expanded = await loop.run_in_executor(executor, expand_query, query)
        logging.info(f"Expanded '{query}' into {len(expanded)} queries: {expanded}")
        # Step 4: Multi-query search — search all variants, merge, and re-rank
        results = await loop.run_in_executor(
            executor, multi_search_protocols, PROTOCOL_INDEX, expanded, req.top_k
        )

    # Step 5: Optionally explain the top results using the local LLM.
    # Skip entirely when Ollama is unreachable — otherwise explain_matches would
    # spend ~15s on doomed retries and stall the request.
    explanation = ""
    if req.explain and results and claude_is_available():
        explanation = await loop.run_in_executor(
            executor, explain_matches, query, results[:3]
        )

    return {
        "query": query,
        "intent": "search",
        "reply": None,
        "explanation": explanation,
        "results": results,
        "expanded_queries": expanded,
        "concepts": concepts,
        "expansions": expansions,
        "sentence_variants": sentence_variants,
        "search_mode": req.search_mode,
        "feedback_prompt": "Were these results relevant? I can help narrow the search.",
        "feedback_options": [
            "Narrow by organism (e.g. plant, human, mouse)",
            "Narrow by technique (e.g. qPCR, western blot, CRISPR)",
            "Narrow by sample type (e.g. tissue, cell line, bacteria)",
            "Narrow by experimental goal (e.g. extraction, detection, quantification)",
        ],
        "total_protocols_indexed": total_indexed,
    }


@app.get("/")
async def root():
    return {
        "message": "Local Ollama RAG backend is running.",
        "routes": [
            "/health",
            "/chat",
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
    custom_prompts: Optional[str] = Form(None),
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

    parsed_prompts = []
    if custom_prompts:
        try:
            parsed_prompts = json.loads(custom_prompts)
            if not isinstance(parsed_prompts, list):
                parsed_prompts = []
        except Exception:
            parsed_prompts = []

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
            "custom_prompts": parsed_prompts,
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
        custom_prompts = options.get("custom_prompts") or []
        if execution_strategy == "prompt_based":
            if custom_prompts:
                _thread_safe_send_update(session_id, f"Starting prompt-based analysis with {len(custom_prompts)} custom prompt(s)...")
            else:
                _thread_safe_send_update(session_id, "Starting prompt-based analysis...")
        else:
            _thread_safe_send_update(session_id, "Starting agentic analysis pipeline...")
        final_output = analyze_target_with_ollama(
            user_query=user_query,
            target_text=target_text,
            retrieved_chunks=retrieved_chunks,
            analysis_mode=options.get("analysis_mode", "compliance"),
            execution_strategy=execution_strategy,
            custom_prompts=custom_prompts,
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
