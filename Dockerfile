# Single-container image for Cloud Run / Hugging Face Spaces / any Docker host.
# Serves the FastAPI backend AND the static frontend on one port, and calls
# Claude (no GPU needed). Set ANTHROPIC_API_KEY at runtime, not here.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install deps first for better layer caching.
COPY requirements.txt .
RUN pip install -r requirements.txt

# Copy the rest of the repo (corpus under data/, backend, frontend).
COPY . .

# Build the TF-IDF index now (CPU is available during the build) and bake the
# pickle into the image. At runtime the app just loads it — Cloud Run throttles
# CPU between requests, so a runtime build would stall and never finish.
RUN python scripts/build_index.py

# Hosted deploys use Claude; local dev keeps Ollama via variables.env.
ENV LLM_PROVIDER=claude \
    CLAUDE_MODEL=claude-sonnet-4-6 \
    EXECUTION_STRATEGY=agentic

# Run from the backend dir so PROTOCOLS_DATA_DIR ("../data/protocols") resolves,
# matching the Render start command. Cloud Run/Spaces inject $PORT (default 8080).
WORKDIR /app/protocolsnerd-backend
ENV PORT=8080
EXPOSE 8080
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}"]
