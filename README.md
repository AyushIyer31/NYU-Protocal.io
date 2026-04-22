# CustomNerd — Local Agentic Document Analysis

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python Version](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-green.svg)](https://fastapi.tiangolo.com/)
[![Ollama](https://img.shields.io/badge/Ollama-local%20LLM-blueviolet.svg)](https://ollama.com/)

CustomNerd is a **fully local**, privacy-first document analysis system. Upload context documents (regulations, policies, standards) and a target document, then let a local Ollama LLM evaluate how well the target aligns with the context. By default it runs a multi-step **agentic** pipeline, and it can also run in a **prompt-based** single-pass mode.

**No data leaves your machine.** All inference runs locally via Ollama.

## How It Works

1. **Upload context documents** — laws, regulations, policies, standards, or any governing documents that define requirements.
2. **Upload a target document** — the specific document you want evaluated against the context.
3. **Describe your query** — tell the system what to check (e.g., "Does this interconnection agreement comply with the uploaded FERC regulations?").
4. Choose an **execution strategy**:
   - **Agentic** (default): Runs a 3-step pipeline.
     - **Step 1 — Summarize**: Produces a concise summary of the target document.
     - **Step 2 — Evaluate**: For each of the top-k retrieved context chunks, asks the LLM a focused question: "Does the target comply with this specific requirement?" Each chunk gets its own LLM call with a simple STATUS / ISSUE / EVIDENCE / EXPLANATION format.
     - **Step 3 — Synthesize**: Takes all individual findings and writes a final verdict with key issues and recommendations.
   - **Prompt-based**: Sends the target document plus the retrieved context chunks in one consolidated prompt and asks the model for a structured report in a single call.
5. Results are rendered in a styled report with color-coded compliance badges, evidence quotes, and a metadata sidebar.

## First Planned Use Case

Evaluating **interconnection documents** for power plant and energy infrastructure projects against applicable regulations and standards.

## Quick Start

### Prerequisites

- **Python 3.11+** ([download](https://www.python.org/downloads/))
- **Ollama** installed and running locally ([download](https://ollama.com/download))
- A pulled model — `llama3.2` (3B) works well; larger models produce better results

### Step 1: Install Ollama and pull a model

```bash
# macOS (also available for Linux and Windows — see https://ollama.com/download)
brew install ollama

# Start the Ollama server
ollama serve

# In a separate terminal, pull a model
ollama pull llama3.2
```

### Step 2: Clone and install Python dependencies

```bash
git clone <repo-url>
cd Customnerd_Agentic

# Create a virtual environment (recommended)
python3 -m venv venv
source venv/bin/activate        # macOS/Linux
# venv\Scripts\activate         # Windows

# Install dependencies
pip install -r requirements.txt
```

### Step 3: Start the application

```bash
python3 run.py
```

This starts:
- **Backend API** on `http://localhost:8000` (FastAPI + Uvicorn)
- **Frontend** on `http://localhost:8080` (Python static file server)
- Opens your browser automatically

### Step 4: Use the app

1. Type a question in the text field (e.g., "Check if this agreement complies with the uploaded regulations").
2. Upload one or more **context documents** (the rules/regulations/policies).
3. Upload a single **target document** (the document to evaluate).
4. Click **Run Analysis** and watch the selected analysis flow progress in real time.

### Running Prompt-Based Instead Of Agentic

1. Start the app with `python3 run.py`.
2. In the UI, change **Execution strategy** from `Agentic` to `Prompt-based`.
3. Upload context and target documents as usual.
4. Click **Run Analysis**.

If you want prompt-based mode to be the default for your local install, set `EXECUTION_STRATEGY=prompt_based` in `customnerd-backend/variables.env`.

### Configuration

Edit `customnerd-backend/variables.env` to change the Ollama model, base URL, or default execution strategy:

```env
LLM=ollama
OLLAMA_MODEL=llama3.2
OLLAMA_BASE_URL=http://localhost:11434
EXECUTION_STRATEGY=agentic
```

Larger models (e.g., `llama3.1:8b`, `qwen3:8b`) produce more detailed analysis but require more RAM and are slower.

Execution strategy options:
- `agentic`: default multi-call summarize/evaluate/synthesize workflow
- `prompt_based`: single-call workflow that relies on one consolidated prompt

## Project Structure

```
customnerd-backend/
  main.py                 # FastAPI app — endpoints, SSE streaming, background processing
  helper_functions.py     # Text extraction, chunking, TF-IDF retrieval, agentic + prompt-based analysis
  ollama_executions.py    # Ollama client wrapper with retry logic
  variables.env           # Environment config (model, base URL, execution strategy)

customnerd-website/
  index.html              # Single-page UI for document upload and analysis
  index.js                # Frontend logic — SSE streaming, HTML report rendering
  index.css               # Styles — report sections, badges, cards
  env.js                  # Frontend configuration (site name, API URL, styling)
  assets/                 # Logo and static assets

run.py                    # Launcher — starts backend + frontend file server
requirements.txt          # Python dependencies (11 packages)
```

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Root — lists available routes |
| `GET` | `/health` | Health check (Ollama reachability, storage status) |
| `GET` | `/sse?session_id=...` | Server-Sent Events stream for a processing session |
| `POST` | `/process_local_rag_analysis` | Main analysis endpoint — accepts query, context files, target file |
| `GET` | `/fetch_backend_mode` | Returns backend mode info and available execution strategies |
| `GET` | `/ollama_status` | Ollama server status and available models |

## Analysis Pipeline Detail

### Text Extraction
PyMuPDF for PDFs, plain-text reader for everything else, with HTML cleaning via BeautifulSoup.

### Chunking
Overlapping character-based chunks (default: 1200 chars, 200 overlap) to preserve nearby context.

### Retrieval
TF-IDF vectorization (unigrams + bigrams) with cosine similarity. The user query and first 2500 chars of the target document form the retrieval query. Top-k (default: 8) most relevant chunks are returned.

### Agentic Analysis (3 steps, multiple LLM calls)
1. **Summarize** — one LLM call to produce a 3-5 sentence target document summary
2. **Evaluate** — one LLM call per retrieved chunk, each asking "does the target comply with this requirement?" in a structured STATUS/ISSUE/EVIDENCE/EXPLANATION format
3. **Synthesize** — one LLM call that reads all findings and writes a final verdict, key issues, and recommendations

This multi-call approach works well even with small models (3B parameters) because each call is focused and simple.

### Prompt-Based Analysis (single LLM call)
1. Retrieve the top-k most relevant context chunks.
2. Build one prompt containing the user question, target document, and retrieved context.
3. Ask the model for a structured JSON report in a single response.

This path is simpler and can be useful when you want a faster, less orchestrated workflow, though it is generally less controlled than the agentic pipeline.

### Streaming
Progress updates for every pipeline step are streamed to the frontend via SSE in real time.

## Privacy

- All processing happens locally on your machine.
- No external API calls are made during analysis.
- Uploaded files are stored temporarily in `storage/sessions/` and can be cleaned up at any time.

## Troubleshooting

**Ollama not running**: Start it with `ollama serve` in a separate terminal.

**Model not found**: Pull it with `ollama pull llama3.2`.

**Backend won't start**: Make sure port 8000 is free and all dependencies are installed (`pip install -r requirements.txt`).

**Empty or generic results**: Try a larger model (`ollama pull llama3.1:8b`) or more specific queries.

## License

MIT License — see [LICENSE](LICENSE) for details.
