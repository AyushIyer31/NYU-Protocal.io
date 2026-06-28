from openai import OpenAI, RateLimitError, APITimeoutError, APIConnectionError, NotFoundError
import os
import time
import logging
import json
import re
from typing import Dict, Optional, Any, List

MAX_RETRIES = 3
BACKOFF_SECS = 2

logging.basicConfig(level=logging.INFO)


# ---------------------------------------------------------------------
# Ollama connection / config
# ---------------------------------------------------------------------

def _get_ollama_model() -> str:
    model = os.getenv("OLLAMA_MODEL", "llama3.2").strip('"').strip()
    return model if model else "llama3.2"


def _get_ollama_base_url() -> str:
    base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").strip('"').strip()
    if not base_url:
        base_url = "http://localhost:11434"
    if not base_url.endswith("/v1") and not base_url.endswith("/v1/"):
        base_url = base_url.rstrip("/") + "/v1/"
    return base_url


def _make_client() -> Optional[OpenAI]:
    try:
        return OpenAI(
            base_url=_get_ollama_base_url(),
            api_key="ollama",  # required by SDK, ignored by Ollama
        )
    except Exception as e:
        logging.error(f"Failed to create Ollama client: {e}")
        return None


client = _make_client()


def reinitialize_ollama_client():
    global client
    client = _make_client()
    if client:
        logging.info(f"Ollama client reinitialized (model={_get_ollama_model()}, url={_get_ollama_base_url()})")
    else:
        logging.warning("Ollama client could not be initialized")


# ---------------------------------------------------------------------
# Provider selection (local Ollama vs hosted Claude)
# ---------------------------------------------------------------------
#
# Local development uses Ollama (no API key, runs on the dev machine).
# Cloud / always-on deployments set LLM_PROVIDER=claude so every LLM call
# routes to Anthropic instead — there is no GPU on a free host to run
# Ollama. Every LLM call in the backend funnels through
# `_retryable_ollama_call`, so this is the only switch point needed.

def active_provider(override: Optional[str] = None) -> str:
    """Return provider: 'openai', 'claude', 'gemini', or 'ollama'. Accepts override."""
    def _classify(p: str) -> str:
        p = p.strip().lower()
        if p in ("openai", "gpt"):
            return "openai"
        if p in ("claude", "anthropic"):
            return "claude"
        if p in ("gemini", "google"):
            return "gemini"
        return "ollama"
    if override:
        return _classify(override)
    return _classify(os.getenv("LLM_PROVIDER", os.getenv("LLM", "openai")).strip('"'))


def _get_openai_model() -> str:
    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip('"').strip()
    return model if model else "gpt-4o-mini"


def _get_claude_model() -> str:
    model = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6").strip('"').strip()
    return model if model else "claude-sonnet-4-6"


def _get_gemini_model() -> str:
    model = os.getenv("GEMINI_MODEL", "gemini-2.0-flash").strip('"').strip()
    return model if model else "gemini-2.0-flash"


_CLAUDE_MAX_TOKENS = int(os.getenv("CLAUDE_MAX_TOKENS", "4096").strip('"').strip() or "4096")

_openai_client = None
_claude_client = None


def _make_openai_client():
    """Lazily build an OpenAI client. Returns None if unavailable."""
    if not os.getenv("OPENAI_API_KEY"):
        logging.warning("OPENAI_API_KEY not set; OpenAI provider unavailable.")
        return None
    try:
        return OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    except Exception as e:
        logging.error(f"Failed to create OpenAI client: {e}")
        return None


def openai_available() -> bool:
    global _openai_client
    if _openai_client is None:
        _openai_client = _make_openai_client()
    return _openai_client is not None


def _make_claude_client():
    """Lazily build an Anthropic client. Returns None if unavailable."""
    if not os.getenv("ANTHROPIC_API_KEY"):
        logging.warning("ANTHROPIC_API_KEY not set; Claude provider unavailable.")
        return None
    try:
        import anthropic
    except ImportError:
        logging.error("anthropic package not installed. Run: pip install anthropic")
        return None
    try:
        return anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env
    except Exception as e:
        logging.error(f"Failed to create Anthropic client: {e}")
        return None


def claude_available() -> bool:
    global _claude_client
    if _claude_client is None:
        _claude_client = _make_claude_client()
    return _claude_client is not None


# ---------------------------------------------------------------------
# Gemini (Google Generative Language API) — REST, no SDK dependency
# ---------------------------------------------------------------------

_GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models"
# certifi CA bundle for consistent TLS across hosts/proxies (matches pubmed_client).
try:
    import certifi as _certifi
    import ssl as _ssl
    _GEMINI_SSL_CTX = _ssl.create_default_context(cafile=_certifi.where())
except Exception:
    _GEMINI_SSL_CTX = None


def _gemini_api_key() -> str:
    return os.getenv("GEMINI_API_KEY", os.getenv("GOOGLE_API_KEY", "")).strip('"').strip()


def gemini_available() -> bool:
    if not _gemini_api_key():
        logging.warning("GEMINI_API_KEY not set; Gemini provider unavailable.")
        return False
    return True


def _retryable_gemini_call(
    *,
    messages,
    temperature=0.3,
    top_p=1.0,
    response_format: Optional[Dict[str, str]] = None,
) -> str:
    """
    Gemini REST call. Translates OpenAI-style `messages` into Gemini's
    systemInstruction + contents shape. Returns response text or "" on failure.
    """
    import json as _json
    import urllib.request as _ur

    key = _gemini_api_key()
    if not key:
        return ""

    system_parts = [m["content"] for m in messages if m.get("role") == "system"]
    system_text = "\n\n".join(p for p in system_parts if p)
    if response_format and response_format.get("type") == "json_object":
        system_text += "\n\nReturn ONLY a single valid JSON value. No markdown fences, no commentary."

    contents = [
        {
            "role": "model" if m.get("role") == "assistant" else "user",
            "parts": [{"text": m.get("content", "")}],
        }
        for m in messages
        if m.get("role") in ("user", "assistant")
    ] or [{"role": "user", "parts": [{"text": ""}]}]

    body: Dict[str, Any] = {
        "contents": contents,
        "generationConfig": {"temperature": temperature, "topP": top_p, "maxOutputTokens": 2048},
    }
    if system_text:
        body["systemInstruction"] = {"parts": [{"text": system_text}]}
    if response_format and response_format.get("type") == "json_object":
        body["generationConfig"]["responseMimeType"] = "application/json"

    url = f"{_GEMINI_ENDPOINT}/{_get_gemini_model()}:generateContent?key={key}"
    data = _json.dumps(body).encode("utf-8")

    for attempt in range(MAX_RETRIES):
        try:
            req = _ur.Request(url, data=data, headers={"Content-Type": "application/json"})
            with _ur.urlopen(req, timeout=30, context=_GEMINI_SSL_CTX) as r:
                payload = _json.loads(r.read().decode("utf-8", errors="ignore"))
            candidates = payload.get("candidates") or []
            if not candidates:
                return ""
            parts = (candidates[0].get("content") or {}).get("parts") or []
            return "".join(p.get("text", "") for p in parts).strip()
        except Exception as e:
            status = getattr(e, "code", None)
            retryable = status in (408, 429, 500, 503) or status is None
            logging.warning(f"[Gemini attempt {attempt + 1}/{MAX_RETRIES}] {e}")
            if not retryable:
                break
            time.sleep(BACKOFF_SECS * (2 ** attempt))

    return ""


def _retryable_openai_call(
    *,
    messages,
    temperature=0.3,
    top_p=1.0,
    response_format: Optional[Dict[str, str]] = None,
) -> str:
    """OpenAI call with retries. Returns response text or "" on failure."""
    global _openai_client
    if _openai_client is None:
        _openai_client = _make_openai_client()
    if _openai_client is None:
        return ""

    model = _get_openai_model()
    kwargs = dict(
        model=model,
        messages=messages,
        temperature=temperature,
        top_p=top_p,
        max_tokens=2048,
    )
    if response_format and response_format.get("type") == "json_object":
        kwargs["response_format"] = {"type": "json_object"}

    for attempt in range(MAX_RETRIES):
        try:
            resp = _openai_client.chat.completions.create(**kwargs)
            return resp.choices[0].message.content or ""
        except Exception as e:
            status = getattr(e, "status_code", None)
            retryable = status in (408, 429, 500, 503) or status is None
            logging.warning(f"[OpenAI attempt {attempt + 1}/{MAX_RETRIES}] {e}")
            if not retryable:
                break
            time.sleep(BACKOFF_SECS * (2 ** attempt))

    return ""


def _retryable_claude_call(
    *,
    messages,
    temperature=0.3,  # accepted for signature parity; not sent to Claude
    top_p=1.0,
    response_format: Optional[Dict[str, str]] = None,
) -> str:
    """
    Claude equivalent of `_retryable_ollama_call`. Translates the OpenAI-style
    `messages` list into Anthropic's system + messages shape and returns the
    response text (or "" on failure).
    """
    global _claude_client
    if _claude_client is None:
        _claude_client = _make_claude_client()
    if _claude_client is None:
        return ""

    # Anthropic takes the system prompt separately from the turn messages.
    system_parts = [m["content"] for m in messages if m.get("role") == "system"]
    convo = [
        {"role": m["role"], "content": m["content"]}
        for m in messages
        if m.get("role") in ("user", "assistant")
    ]
    system = "\n\n".join(p for p in system_parts if p)
    if response_format and response_format.get("type") == "json_object":
        system += "\n\nReturn ONLY a single valid JSON value. No markdown fences, no commentary."

    # Note: Opus 4.8 rejects temperature/top_p (400), so we omit sampling
    # params entirely — omitting is valid on every Claude model. Thinking is
    # left off (omitted) for fast, JSON-clean structured replies.
    for attempt in range(MAX_RETRIES):
        try:
            resp = _claude_client.messages.create(
                model=_get_claude_model(),
                max_tokens=_CLAUDE_MAX_TOKENS,
                system=system or "You are a helpful assistant.",
                messages=convo or [{"role": "user", "content": ""}],
            )
            return "".join(b.text for b in resp.content if b.type == "text").strip()
        except Exception as e:
            # Retry on transient errors (rate limit / overload / 5xx / network),
            # give up on the rest.
            status = getattr(e, "status_code", None)
            retryable = status in (408, 409, 429, 500, 529) or status is None
            logging.warning(f"[Claude attempt {attempt + 1}/{MAX_RETRIES}] {e}")
            if not retryable:
                break
            time.sleep(BACKOFF_SECS * (2 ** attempt))

    return ""


# ---------------------------------------------------------------------
# Core API call
# ---------------------------------------------------------------------

def _retryable_ollama_call(
    *,
    messages,
    temperature=0.3,
    top_p=1.0,
    response_format: Optional[Dict[str, str]] = None,
    provider: Optional[str] = None,
) -> str:
    """
    Retry wrapper for LLM calls. Routes based on provider:
    - 'openai': OpenAI API (gpt-4o-mini by default)
    - 'claude': Anthropic Claude API
    - 'gemini': Google Gemini API (gemini-2.0-flash by default)
    - 'ollama': Local Ollama server
    Returns raw text content or "" on failure.
    """
    resolved_provider = active_provider(override=provider)

    if resolved_provider == "openai":
        return _retryable_openai_call(
            messages=messages,
            temperature=temperature,
            top_p=top_p,
            response_format=response_format,
        )

    if resolved_provider == "claude":
        return _retryable_claude_call(
            messages=messages,
            temperature=temperature,
            top_p=top_p,
            response_format=response_format,
        )

    if resolved_provider == "gemini":
        return _retryable_gemini_call(
            messages=messages,
            temperature=temperature,
            top_p=top_p,
            response_format=response_format,
        )

    if not client:
        logging.error("Ollama client not initialized.")
        return ""

    model = _get_ollama_model()
    kwargs = dict(
        model=model,
        messages=messages,
        temperature=temperature,
        top_p=top_p,
    )
    if response_format:
        kwargs["response_format"] = response_format

    for attempt in range(MAX_RETRIES):
        try:
            resp = client.chat.completions.create(**kwargs)
            return resp.choices[0].message.content or ""
        except (RateLimitError, APITimeoutError, APIConnectionError) as e:
            logging.warning(f"[Ollama attempt {attempt + 1}/{MAX_RETRIES}] {e}")
            time.sleep(BACKOFF_SECS * (2 ** attempt))
        except NotFoundError:
            logging.error(f"Ollama model '{model}' not found. Run: ollama pull {model}")
            break
        except Exception as e:
            logging.exception(f"[Ollama fatal] {e}")
            break

    return ""
