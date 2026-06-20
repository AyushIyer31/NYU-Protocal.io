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
# Core API call
# ---------------------------------------------------------------------

def _retryable_ollama_call(
    *,
    messages,
    temperature=0.3,
    top_p=1.0,
    response_format: Optional[Dict[str, str]] = None,
) -> str:
    """
    Retry wrapper for local Ollama calls.
    Returns raw text content or "" on failure.
    """
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
