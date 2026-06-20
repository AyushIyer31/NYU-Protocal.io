# ProtocolsNerd — Local Agentic Document Analysis

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python Version](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-green.svg)](https://fastapi.tiangolo.com/)
[![Ollama](https://img.shields.io/badge/Ollama-local%20LLM-blueviolet.svg)](https://ollama.com/)

ProtocolsNerd is a **fully local**, privacy-first document analysis system. Upload context documents (regulations, policies, standards) and a target document, then let a local Ollama LLM evaluate how well the target aligns with the context. It supports two execution modes: a multi-step **agentic** pipeline and a **prompt-based** mode that runs a set of user-defined checks.

**No data leaves your machine.** All inference runs locally via Ollama.

## How It Works

1. **Upload context documents** — laws, regulations, policies, standards, or any governing documents that define requirements.
2. **Upload a target document** — the specific document you want evaluated against the context.
3. **Describe your query** — tell the system what to check (e.g., "Does this interconnection agreement comply with the uploaded FERC regulations?").
4. Choose an **execution strategy**:
   - **Agentic** (default): Runs a 3-step pipeline.
     - **Step 1 — Summarize**: Produces a concise summary of the target document.
     - **Step 2 — Evaluate**: For each of the top-k retrieved context chunks, asks the LLM a focused question: "Does the target comply with this specific requirement?" Each chunk gets its own LLM call with a structured STATUS / ISSUE / EVIDENCE / EXPLANATION format.
     - **Step 3 — Synthesize**: Takes all individual findings and writes a final verdict with key issues and recommendations.
   - **Prompt-based**: Runs one or more user-defined checks against the target document and retrieved context. See [Prompt-Based Mode](#prompt-based-mode) below.
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
4. Select an **execution strategy** and click **Run Analysis**.

---

## Agentic Mode

Agentic mode is the default. It breaks the analysis into three focused LLM calls, which works well even with small (3B) models because each call is narrow and simple.

**Pipeline:**
1. **Summarize** — one LLM call produces a 3–5 sentence summary of the target document.
2. **Evaluate** — one LLM call per retrieved context chunk (default: 8 chunks), each asking "does the target satisfy this specific requirement?" and returning a structured STATUS / ISSUE / EVIDENCE / EXPLANATION response.
3. **Synthesize** — one final LLM call reads all chunk-level findings and writes an overall verdict, key issues, and recommendations.

**When to use it:**
- General-purpose compliance review where you want broad coverage of the context documents.
- When you don't know in advance which specific aspects to check.
- When working with smaller models — the narrow per-chunk calls keep each LLM task manageable.

---

## Prompt-Based Mode

Prompt-based mode lets you define the exact checks you want the model to run. Instead of the system deciding what to evaluate, you supply a list of specific prompts — one per check — and the model evaluates each one independently against the target document and the retrieved context.

**How it works:**

Each prompt is sent as its own LLM call with the full target document excerpt and the top retrieved context chunks. The model returns a structured finding for that specific check: a status (compliant / non-compliant / unclear), explanation, evidence quotes, and a recommendation. All findings are assembled into the same report format as agentic mode.

**When to use it:**
- You have domain expertise and know exactly what to look for (e.g., a checklist of regulatory requirements).
- You want consistent, repeatable checks across many documents.
- You want the report to map directly to a specific review checklist or standard.
- You need to audit or explain each check individually.

### Using Prompt-Based Mode

1. Start the app with `python3 run.py`.
2. In the UI, change **Execution strategy** from `Agentic` to `Prompt-based`. A **Custom Analysis Prompts** panel will appear.
3. Add your prompts in one of two ways:
   - **Type them manually** — enter a prompt in the text box and click **Add** (or press Enter). Repeat for each check.
   - **Upload a prompts file** — click "Choose prompts file" and upload a `.txt` file (one prompt per line) or a `.json` file (an array of strings). All prompts in the file are added to the list at once.
4. Review the prompt list. Use the × button to remove any prompt, or **Clear all** to start over.
5. Upload your context and target documents as usual, then click **Run Analysis**.

If no prompts are added, prompt-based mode falls back to a single consolidated pass that sends the full context and target to the model in one call.

### Writing Good Prompts

Each prompt should describe one specific, self-contained check. The model works best when a prompt is:

- **Specific** — name the exact attribute, clause type, or requirement to check.
- **Scoped** — one thing per prompt, not "check everything about metering."
- **Actionable** — frame it as something the model can look for in the text.

Examples of well-scoped prompts:
```
Check that all voltage levels are stated explicitly in kV and are consistent with the interconnection point specifications.

Check that the agreement defines a Point of Interconnection with enough detail (substation, bus, or circuit) to unambiguously locate it.

Check that force majeure provisions clearly state whether they excuse payment obligations or only performance obligations.
```
---

## Agentic vs. Prompt-Based: Comparison

| | Agentic | Prompt-Based |
|---|---|---|
| **Who defines the checks** | The system (driven by retrieved chunks) | You (explicit prompts) |
| **LLM calls per run** | 2 + number of retrieved chunks (default: 10) | 1 per prompt |
| **Best for** | Broad, exploratory review | Targeted, checklist-driven review |
| **Works well with small models** | Yes — each call is narrow | Yes — each call is focused |
| **Output** | Findings keyed to context chunks | Findings keyed to your prompts |
| **Repeatability** | Varies with chunk retrieval | Consistent across documents |

---

## Configuration

Edit `protocolsnerd-backend/variables.env` to change the Ollama model, base URL, or default execution strategy:

```env
LLM=ollama
OLLAMA_MODEL=llama3.2
OLLAMA_BASE_URL=http://localhost:11434
EXECUTION_STRATEGY=agentic
```

Larger models (e.g., `llama3.1:8b`, `qwen3:8b`) produce more detailed analysis but require more RAM and are slower.

Execution strategy options:
- `agentic`: default multi-call summarize/evaluate/synthesize workflow
- `prompt_based`: user-defined prompt checks, or single-pass if no prompts are supplied

---

## Project Structure

```
protocolsnerd-backend/
  main.py                 # FastAPI app — endpoints, SSE streaming, background processing
  helper_functions.py     # Text extraction, chunking, TF-IDF retrieval, agentic + prompt-based analysis
  ollama_executions.py    # Ollama client wrapper with retry logic
  variables.env           # Environment config (model, base URL, execution strategy)

protocolsnerd-website/
  index.html              # Single-page UI for document upload and analysis
  index.js                # Frontend logic — SSE streaming, HTML report rendering, prompt management
  index.css               # Styles — report sections, badges, cards, prompts panel
  env.js                  # Frontend configuration (site name, API URL, styling)
  assets/                 # Logo and static assets

sample-docs/
  sample-prompts-interconnection-ny.txt   # 15 sample prompts for NY interconnection agreements

run.py                    # Launcher — starts backend + frontend file server
requirements.txt          # Python dependencies
```

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Root — lists available routes |
| `GET` | `/health` | Health check (Ollama reachability, storage status) |
| `GET` | `/sse?session_id=...` | Server-Sent Events stream for a processing session |
| `POST` | `/process_local_rag_analysis` | Main analysis endpoint — accepts query, context files, target file, optional custom prompts |
| `GET` | `/fetch_backend_mode` | Returns backend mode info and available execution strategies |
| `GET` | `/ollama_status` | Ollama server status and available models |

## Analysis Pipeline Detail

### Text Extraction
PyMuPDF for PDFs, plain-text reader for everything else, with HTML cleaning via BeautifulSoup.

### Chunking
Overlapping character-based chunks (default: 1200 chars, 200 overlap) to preserve nearby context.

### Retrieval
TF-IDF vectorization (unigrams + bigrams) with cosine similarity. The user query and first 2500 chars of the target document form the retrieval query. Top-k (default: 8) most relevant chunks are returned and passed to both agentic and prompt-based analysis.

### Agentic Analysis (3 steps, multiple LLM calls)
1. **Summarize** — one LLM call to produce a 3–5 sentence target document summary
2. **Evaluate** — one LLM call per retrieved chunk, each asking "does the target comply with this requirement?" in a structured STATUS/ISSUE/EVIDENCE/EXPLANATION format
3. **Synthesize** — one LLM call that reads all findings and writes a final verdict, key issues, and recommendations

### Prompt-Based Analysis (one LLM call per prompt)
1. Retrieve the top-k most relevant context chunks.
2. For each user-supplied prompt, run one LLM call with the prompt, target document, and retrieved context.
3. Each call returns a structured finding: status, explanation, evidence, and recommendation.
4. All findings are assembled into the standard report format.

If no prompts are supplied, prompt-based mode sends the full target document and retrieved context in one consolidated call and asks for a structured JSON report.

### Streaming
Progress updates for every pipeline step — including per-prompt progress — are streamed to the frontend via SSE in real time.

## Privacy

- All processing happens locally on your machine.
- No external API calls are made during analysis.
- Uploaded files are stored temporarily in `storage/sessions/` and can be cleaned up at any time.

## Troubleshooting

**Ollama not running**: Start it with `ollama serve` in a separate terminal.

**Model not found**: Pull it with `ollama pull llama3.2`.

**Backend won't start**: Make sure port 8000 is free and all dependencies are installed (`pip install -r requirements.txt`).

**Empty or generic results**: Try a larger model (`ollama pull llama3.1:8b`) or more specific queries.

**"Ollama client not initialized" error**: Your `openai` package version is incompatible with your installed `httpx`. Fix it by running `pip install "openai>=1.52.0"`.

---

## Protocols.io Chatbot (NYU Project)

This section documents the **protocol search assistant** built on top of ProtocolsNerd for the NYU research project with Prof. Shasha. The goal is to let bench scientists find lab protocols on protocols.io using natural language.

For hosted deployment instructions, see [DEPLOYMENT.md](DEPLOYMENT.md). The repo includes `render.yaml` for a Render backend plus Render static frontend, and `netlify.toml` if you want Netlify for the frontend.

### How to run the chatbot

```bash
# 1. Fetch and cache protocols (only needs to run once; resumable)
python3 fetch_protocols.py

# 2. Start backend + frontend
python3 run.py
# Opens http://localhost:5555/chat.html automatically
```

Ollama is optional. If installed and running (`ollama serve`), the system will generate plain-English explanations of why protocols match. Without Ollama, search still works fully — explanations are just skipped.

### How TF-IDF search works

Each cached protocol (title, description, guidelines, steps, materials, keywords) is converted to a single text blob and indexed with scikit-learn's `TfidfVectorizer`. At query time, the user's query is vectorized and compared to every protocol using cosine similarity. The top-k highest-scoring protocols are returned.

**Why TF-IDF is limited:**
TF-IDF is keyword-based — it matches on exact or near-exact words. If the user says "isolate messenger RNA" but the protocol title says "mRNA extraction", the scores will be low because the words don't overlap. It does not understand meaning, only vocabulary.

### How the clarification layer helps

Before searching, the chatbot checks whether the query is too vague to produce good results (e.g. "PCR", "overexpression", "cell culture"). If it is, the chatbot asks one targeted follow-up question instead of returning weak results. For example:

> User: "I want to test overexpression"
> Assistant: "Do you mean gene overexpression, protein overexpression, or overexpression in a specific system (e.g. mammalian cells, plants, bacteria)?"

This is implemented as a rule-based check in `protocol_rag.py` (`is_vague_query`, `get_clarification_question`).

### How multiple query generation improves search

After a valid query is received, the system expands it into 3–5 related search variants using synonym substitution (e.g. extraction → isolation → purification) or, when Ollama is running, by asking the LLM to rephrase. All variants are searched against the TF-IDF index and the results are merged.

**Re-ranking formula:**
```
combined_score = best_tfidf_score + 0.05 × (number of query variants that matched − 1)
```

Protocols that appear across multiple query variants get a small frequency bonus, which rewards broad relevance over narrow keyword coincidence.

### What still needs improvement

- **Semantic search**: Replace or augment TF-IDF with sentence-transformer embeddings so that meaning-level matches (not just vocabulary matches) are found.
- **Dataset coverage**: 962 protocols is a small slice of protocols.io. A more principled crawl strategy is needed.
- **Multi-turn conversation memory**: Each query is currently stateless. Future work should let users say "filter those to plant-based ones" referencing prior results.
- **Feedback loop learning**: Clicks on feedback buttons do not yet update the ranking model. User signals could eventually improve result quality.

---

## License

MIT License — see [LICENSE](LICENSE) for details.
