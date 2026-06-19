"""
Claude API client for the protocol search chatbot.

Replaces local Ollama with the Anthropic Claude API for all LLM tasks:
  - generate_search_queries()   DietNerd-style multi-probe query expansion
  - classify_intent()           chitchat vs. protocol search
  - explain_matches()           plain-English explanation of why results match
  - get_synonyms()              biomedical synonym generation
  - get_sentence_variants()     reworded query variants

Uses claude-haiku-4-5 for all calls — fast, cheap, and more than capable for
these short-input/short-output tasks. Falls back gracefully if the key is missing.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import anthropic
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / "variables.env", override=False)

log = logging.getLogger(__name__)

_MODEL = "claude-haiku-4-5"   # fast + cheap for high-frequency search tasks
_CLIENT: Optional[anthropic.Anthropic] = None


def _get_client() -> Optional[anthropic.Anthropic]:
    global _CLIENT
    if _CLIENT is not None:
        return _CLIENT
    key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not key:
        log.warning("ANTHROPIC_API_KEY not set — Claude features disabled.")
        return None
    try:
        _CLIENT = anthropic.Anthropic(api_key=key)
        return _CLIENT
    except Exception as e:
        log.warning(f"Could not create Anthropic client: {e}")
        return None


def _call(system: str, user: str, max_tokens: int = 512) -> str:
    """Single Claude API call. Returns empty string on failure."""
    client = _get_client()
    if not client:
        return ""
    try:
        resp = client.messages.create(
            model=_MODEL,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return next((b.text for b in resp.content if b.type == "text"), "")
    except Exception as e:
        log.warning(f"Claude API call failed: {e}")
        return ""


def is_available() -> bool:
    """True when the Claude API key is configured."""
    return bool(os.getenv("ANTHROPIC_API_KEY", "").strip())


# ---------------------------------------------------------------------------
# 1. Query generation (the DietNerd approach)
# ---------------------------------------------------------------------------

def generate_search_queries(query: str, n_probes: int = 5) -> List[str]:
    """
    Convert a natural-language query into N short search probes for protocols.io.

    Mirrors DietNerd's query_generation() approach: ask the LLM to produce
    multiple targeted search terms from different angles of the same question,
    then search all of them and merge results.

    Example:
      "Find protocols that allow more than one transcription factor to be
       modified at the same time for mice"
      ->
      ["multiplex CRISPR mice",
       "transcription factor editing",
       "simultaneous gene knockout",
       "multiplex genome editing Mus musculus",
       "combinatorial CRISPR knockout"]
    """
    raw = _call(
        system=(
            "You are a biomedical search expert. Convert the scientist's question into "
            f"{n_probes} short, precise search phrases for a protocol database. "
            "Rules:\n"
            "- Each phrase must be 1-4 words, no punctuation\n"
            "- Use proper scientific terms (e.g. 'Mus musculus' not just 'mice')\n"
            "- Cover different angles: technique, organism, molecule, goal\n"
            "- Most specific phrases first\n"
            f"Return ONLY a JSON array of {n_probes} strings, no explanation.\n\n"
            "Example:\n"
            'Input: "RNA extraction from plant leaves"\n'
            'Output: ["RNA extraction plant", "total RNA isolation", '
            '"plant RNA protocol", "Arabidopsis RNA", "RNA purification leaves"]'
        ),
        user=query,
        max_tokens=256,
    )
    if raw:
        try:
            arr = json.loads(raw.strip())
            if isinstance(arr, list):
                return [str(x).strip() for x in arr if x][:n_probes]
        except Exception:
            pass
    return []


# ---------------------------------------------------------------------------
# 2. Intent classification
# ---------------------------------------------------------------------------

def classify_intent(query: str) -> Dict[str, Any]:
    """
    Decide whether query is a protocol search or general conversation.
    Returns {"intent": "search"|"chitchat", "reply": str|None}
    """
    raw = _call(
        system=(
            "Classify the user's message for a lab protocol search assistant.\n\n"
            "If it is a request to FIND or SEARCH for a lab protocol, experiment method, "
            "or scientific procedure: reply exactly with: SEARCH\n\n"
            "If it is general conversation, a greeting, a question about you, or anything "
            "unrelated to lab protocols: reply with: CHITCHAT | <1-2 sentence friendly response "
            "that mentions what you can help with>\n\n"
            "Examples:\n"
            "  'RNA extraction from plant tissue' -> SEARCH\n"
            "  'hi there' -> CHITCHAT | Hi! Describe an experiment and I'll find the right protocol for you.\n"
            "  'what do you do' -> CHITCHAT | I help scientists find lab protocols from protocols.io. Just describe your experiment!\n"
            "  'western blot protocol' -> SEARCH\n"
            "  'thanks' -> CHITCHAT | You're welcome! Let me know if you need another protocol.\n"
            "  'CRISPR knockout in mice' -> SEARCH"
        ),
        user=query,
        max_tokens=128,
    )
    if raw:
        raw = raw.strip()
        if raw.upper().startswith("SEARCH"):
            return {"intent": "search", "reply": None}
        if raw.upper().startswith("CHITCHAT"):
            parts = raw.split("|", 1)
            reply = parts[1].strip() if len(parts) > 1 else (
                "I'm a lab protocol search assistant. Describe an experiment and I'll find the best matching protocols from protocols.io!"
            )
            return {"intent": "chitchat", "reply": reply}
    # Fallback: keyword heuristic
    return _keyword_fallback(query)


_LAB_KEYWORDS = {
    "protocol", "extraction", "isolation", "purification", "assay", "pcr", "rna", "dna",
    "protein", "cell", "tissue", "buffer", "gel", "blot", "western", "elisa", "crispr",
    "transfection", "sequencing", "microscopy", "culture", "staining", "antibody", "primer",
    "cloning", "transformation", "centrifuge", "incubate", "lysis", "pellet", "supernatant",
    "plasmid", "enzyme", "reagent", "sample", "experiment", "lab", "bacteria", "yeast",
    "arabidopsis", "mouse", "human", "plant", "mammalian", "fluorescence",
}


def _keyword_fallback(query: str) -> Dict[str, Any]:
    words = set(query.lower().split())
    if words & _LAB_KEYWORDS:
        return {"intent": "search", "reply": None}
    return {
        "intent": "chitchat",
        "reply": "I'm a lab protocol search assistant. Describe an experiment you need to run and I'll find the best matching protocols from protocols.io!",
    }


# ---------------------------------------------------------------------------
# 3. Result explanation
# ---------------------------------------------------------------------------

def explain_matches(query: str, results: List[Dict[str, Any]]) -> str:
    """
    Plain-English explanation of why the top protocols match the query.
    """
    protocol_summaries = ""
    for i, r in enumerate(results[:3], 1):
        protocol_summaries += (
            f"\nProtocol {i}: {r.get('title', '')}\n"
            f"  Description: {(r.get('description') or '')[:200]}\n"
            f"  Materials: {(r.get('materials_text') or '')[:120]}\n"
            f"  Why it ranked: {r.get('why', '')}\n"
        )

    return _call(
        system=(
            "You are a helpful lab assistant for bench scientists. A scientist has asked "
            "a question and the system retrieved the top matching protocols from protocols.io. "
            "Explain in 2-4 plain sentences which protocols are most relevant and why, "
            "being specific about what makes each one a good match. "
            "Do not invent steps or materials not mentioned. Keep it concise."
        ),
        user=f"Scientist's request: {query}\n\nTop matching protocols:{protocol_summaries}",
        max_tokens=300,
    )


# ---------------------------------------------------------------------------
# 4. Synonym generation (for concept expansion)
# ---------------------------------------------------------------------------

def get_synonyms(term: str, max_terms: int = 3) -> List[str]:
    """
    Generate biomedical synonyms for a concept — replaces Ollama synonym generation.
    """
    raw = _call(
        system=(
            "You are a biomedical search assistant. Given one concept, reply with ONLY "
            "a JSON array of up to 3 short (1-3 word) synonyms or closely related search "
            "terms a protocol database would index. No explanation.\n"
            'Example: concept "drought tolerance" -> '
            '["drought stress", "water deficit", "dehydration tolerance"]'
        ),
        user=term,
        max_tokens=128,
    )
    if raw:
        try:
            arr = json.loads(raw.strip())
            if isinstance(arr, list):
                return [str(x) for x in arr if isinstance(x, str)][:max_terms]
        except Exception:
            pass
    return []


# ---------------------------------------------------------------------------
# 5. Sentence variant generation (for concept expansion)
# ---------------------------------------------------------------------------

def get_sentence_variants(query: str, n: int = 10) -> List[str]:
    """
    Generate N reworded full-sentence versions of the query.
    """
    raw = _call(
        system=(
            f"Rewrite the user's protocol search request as {n} alternative full-sentence "
            "search queries that mean the same thing, using different scientific phrasing "
            "and synonyms. Reply with ONLY a JSON array of strings, no explanation."
        ),
        user=query,
        max_tokens=512,
    )
    if raw:
        try:
            arr = json.loads(raw.strip())
            if isinstance(arr, list):
                return [str(x) for x in arr if isinstance(x, str)][:n]
        except Exception:
            pass
    return []
