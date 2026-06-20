"""
Ollama-backed LLM client for the protocol search chatbot.

The module name is kept for compatibility with existing imports, but all calls
now go to the local Ollama server configured in variables.env:

LLM=ollama
OLLAMA_MODEL=llama3.2
OLLAMA_BASE_URL=http://localhost:11434
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

from biology_intents import INTENT_FAMILIES, SUB_INTENTS
from ollama_executions import (
    _get_ollama_model,
    _get_claude_model,
    _retryable_ollama_call,
    active_provider,
    claude_available,
)

load_dotenv(Path(__file__).parent / "variables.env", override=False)

log = logging.getLogger(__name__)


def _native_ollama_base_url() -> str:
    base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").strip('"').strip()
    if not base_url:
        base_url = "http://localhost:11434"
    base_url = base_url.rstrip("/")
    if base_url.endswith("/v1"):
        base_url = base_url[:-3].rstrip("/")
    return base_url


def active_model() -> str:
    return _get_claude_model() if active_provider() == "claude" else _get_ollama_model()


def is_available() -> bool:
    """True when the active LLM provider is configured and reachable."""
    if active_provider() == "claude":
        return claude_available()
    try:
        with urllib.request.urlopen(f"{_native_ollama_base_url()}/api/tags", timeout=1.5):
            return True
    except Exception:
        return False


def _call(
    system: str,
    user: str,
    max_tokens: int = 512,
    response_format: Optional[Dict[str, str]] = None,
    temperature: float = 0.2,
) -> str:
    """Single local Ollama call. Returns empty string on failure."""
    if not is_available():
        return ""
    raw = _retryable_ollama_call(
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=temperature,
        response_format=response_format,
    )
    return (raw or "").strip()


def _safe_json_object(raw: str) -> Dict[str, Any]:
    if not raw:
        return {}
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        cleaned = cleaned.removeprefix("json").strip()
    try:
        parsed = json.loads(cleaned)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        pass

    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end > start:
        try:
            parsed = json.loads(raw[start:end + 1])
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _safe_json_array(raw: str) -> List[Any]:
    if not raw:
        return []
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        cleaned = cleaned.removeprefix("json").strip()
    try:
        parsed = json.loads(cleaned)
        return parsed if isinstance(parsed, list) else []
    except Exception:
        pass

    start = raw.find("[")
    end = raw.rfind("]")
    if start != -1 and end > start:
        try:
            parsed = json.loads(raw[start:end + 1])
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []
    return []


def analyze_experiment_request(
    *,
    user_query: str,
    conversation_query: str = "",
    previous_profile: Optional[Dict[str, Any]] = None,
    max_queries: int = 5,
) -> Dict[str, Any]:
    """
    LLM-first planner for the protocol chatbot.

    Returns the structured JSON contract used by /chat:
      intent_family, sub_intent, experiment_profile, missing_fields, next_action,
      clarifying_question, candidate_search_queries.
    """
    if not is_available():
        return {}

    schema = {
        "intent_family": f"one of: {', '.join(sorted(INTENT_FAMILIES))}",
        "sub_intent": f"one of: {', '.join(sorted(SUB_INTENTS))}",
        "intent": {
            "name": "same value as sub_intent for compatibility",
            "label": "human-readable label",
            "confidence": 0.0,
        },
        "experiment_profile": {
            "intent_family": None,
            "sub_intent": None,
            "organism": None,
            "sample_type": None,
            "tissue_or_cell_type": None,
            "target": None,
            "gene_or_construct": None,
            "modification_type": None,
            "method": None,
            "experimental_method": None,
            "delivery_method": None,
            "expression_type": None,
            "readout": None,
            "readout_assay": None,
            "condition": None,
            "timeline": None,
            "equipment": [],
            "required_equipment": [],
            "difficulty": None,
            "protocol_difficulty": None,
            "constraints": [],
            "intent_specific": {},
        },
        "missing_fields": [],
        "next_action": "ask_clarification|generate_search_queries|respond_chitchat",
        "clarifying_question": {
            "field": "field_name_or_null",
            "question": "short question or null",
            "options": [],
        },
        "candidate_search_queries": [],
        "reply": None,
    }

    raw = _call(
        system=(
            "You are a biology protocol-search planning agent running locally in Ollama. "
            "Your job is to turn a scientist's natural-language request and prior chat "
            "state into a structured experiment profile and a next action.\n\n"
            "Return ONLY valid JSON. No markdown. No commentary.\n\n"
            "Allowed next_action values:\n"
            "- ask_clarification: use when a required search field is missing.\n"
            "- generate_search_queries: use when enough information exists to propose protocol-search queries.\n"
            "- respond_chitchat: use only for greetings or non-protocol conversation.\n\n"
            "Controlled intent rules:\n"
            "- Use exactly one allowed intent_family and one allowed sub_intent from the schema.\n"
            "- gene_modification means the user wants genes modified but did not specify the mechanism.\n"
            "- multiplex_gene_modification means more than one gene/target is modified at the same time.\n"
            "- gene_overexpression requires explicit words like overexpress, overexpression, or transgene expression.\n"
            "- genome_editing requires explicit CRISPR/Cas/base-editing/prime-editing language.\n"
            "- stress_tolerance_assay is for drought, salt, heat, cold, or other stress tolerance tests.\n\n"
            "Clarification rules:\n"
            "- Ask exactly one question at a time.\n"
            "- Prefer short answer options when possible.\n"
            "- If the user says genes are modified, gene modification, gene editing, or genome editing without specifying how, use intent gene_modification and ask for modification_type first.\n"
            "- Do not treat ambiguous 'modified genes' as overexpression unless the user explicitly says overexpress or overexpression.\n"
            "- For multiplex gene modification, ask modification_type before searching.\n"
            "- For drought/stress tolerance, ask organism first, then growth_stage/sample/treatment if missing.\n"
            "- For plant gene overexpression, prioritize missing fields in this order: "
            "organism, expression_type, tissue_or_cell_type, readout_assay.\n\n"
            "Search-query rules:\n"
            f"- If next_action is generate_search_queries, provide 3-{max_queries} candidate_search_queries.\n"
            "- Candidate queries should be short phrases suitable for protocols.io, not full sentences.\n"
            "- Generate variants using common/scientific organism names, method synonyms, tissue/sample, and readout/assay terms.\n"
            "- Preserve organism + sub_intent + critical intent_specific concepts in every candidate query.\n"
            "- Do not invent gene names, equipment, or constraints the user did not provide.\n\n"
            "Use this exact schema shape:\n"
            f"{json.dumps(schema, indent=2)}"
        ),
        user=json.dumps(
            {
                "current_user_message": user_query,
                "conversation_query": conversation_query,
                "previous_experiment_profile": previous_profile or {},
            },
            ensure_ascii=False,
            indent=2,
        ),
        max_tokens=900,
        response_format={"type": "json_object"},
        temperature=0.1,
    )
    return _safe_json_object(raw)


def generate_search_queries(query: str, n_probes: int = 5) -> List[str]:
    """
    Convert a natural-language query into compact search probes for protocols.io.
    """
    raw = _call(
        system=(
            "You are a biomedical search expert. Convert the scientist's question into "
            f"{n_probes} short, precise search phrases for a protocol database. "
            "Rules:\n"
            "- Each phrase must be 1-6 words, no punctuation\n"
            "- Use proper scientific terms when useful\n"
            "- Cover different angles: technique, organism, molecule, goal\n"
            "- Most specific phrases first\n"
            f"Return ONLY a JSON array of {n_probes} strings, no explanation."
        ),
        user=query,
        max_tokens=256,
        temperature=0.2,
    )
    return [str(x).strip() for x in _safe_json_array(raw) if str(x).strip()][:n_probes]


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
            "unrelated to lab protocols: reply with: CHITCHAT | <1-2 sentence response "
            "that mentions what you can help with>\n\n"
            "Examples:\n"
            "  'RNA extraction from plant tissue' -> SEARCH\n"
            "  'hi there' -> CHITCHAT | Hi. Describe an experiment and I will find matching protocols.\n"
            "  'western blot protocol' -> SEARCH\n"
            "  'thanks' -> CHITCHAT | You're welcome. Send another experimental goal when ready."
        ),
        user=query,
        max_tokens=128,
        temperature=0.0,
    )
    if raw:
        raw = raw.strip()
        if raw.upper().startswith("SEARCH"):
            return {"intent": "search", "reply": None}
        if raw.upper().startswith("CHITCHAT"):
            parts = raw.split("|", 1)
            reply = parts[1].strip() if len(parts) > 1 else (
                "I'm a lab protocol search assistant. Describe an experiment and I will find matching protocols from protocols.io."
            )
            return {"intent": "chitchat", "reply": reply}
    return _keyword_fallback(query)


_LAB_KEYWORDS = {
    "protocol", "extraction", "isolation", "purification", "assay", "pcr", "rna", "dna",
    "protein", "cell", "tissue", "buffer", "gel", "blot", "western", "elisa", "crispr",
    "transfection", "sequencing", "microscopy", "culture", "staining", "antibody", "primer",
    "cloning", "transformation", "centrifuge", "incubate", "lysis", "pellet", "supernatant",
    "plasmid", "enzyme", "reagent", "sample", "experiment", "lab", "bacteria", "yeast",
    "arabidopsis", "mouse", "human", "plant", "mammalian", "fluorescence", "overexpress",
    "overexpression", "knockdown", "gene",
}


def _keyword_fallback(query: str) -> Dict[str, Any]:
    words = set(query.lower().split())
    if words & _LAB_KEYWORDS:
        return {"intent": "search", "reply": None}
    return {
        "intent": "chitchat",
        "reply": "I'm a lab protocol search assistant. Describe an experiment you need to run and I will find matching protocols from protocols.io.",
    }


def explain_matches(query: str, results: List[Dict[str, Any]]) -> str:
    """
    Plain-English explanation of why the top protocols match the query.
    """
    protocol_summaries = ""
    for i, result in enumerate(results[:3], 1):
        protocol_summaries += (
            f"\nProtocol {i}: {result.get('title', '')}\n"
            f"  Description: {(result.get('description') or '')[:200]}\n"
            f"  Materials: {(result.get('materials_text') or '')[:120]}\n"
            f"  Why it ranked: {result.get('why', '')}\n"
        )

    return _call(
        system=(
            "You are a helpful lab assistant for bench scientists. A scientist asked "
            "a question and the system retrieved matching protocols from protocols.io. "
            "Explain in 2-4 plain sentences which protocols are most relevant and why. "
            "Do not invent steps or materials not mentioned."
        ),
        user=f"Scientist's request: {query}\n\nTop matching protocols:{protocol_summaries}",
        max_tokens=300,
        temperature=0.2,
    )


def get_synonyms(term: str, max_terms: int = 3) -> List[str]:
    """
    Generate biomedical synonyms for a search concept.
    """
    raw = _call(
        system=(
            "You are a biomedical search assistant. Given one concept, reply with ONLY "
            "a JSON array of up to 3 short synonyms or closely related search terms. "
            "No explanation."
        ),
        user=term,
        max_tokens=128,
        temperature=0.2,
    )
    return [str(x).strip() for x in _safe_json_array(raw) if str(x).strip()][:max_terms]


def get_sentence_variants(query: str, n: int = 10) -> List[str]:
    """
    Generate reworded full-sentence versions of the query.
    """
    raw = _call(
        system=(
            f"Rewrite the user's protocol search request as {n} alternative full-sentence "
            "search queries that mean the same thing, using different scientific phrasing "
            "and synonyms. Reply with ONLY a JSON array of strings, no explanation."
        ),
        user=query,
        max_tokens=512,
        temperature=0.3,
    )
    return [str(x).strip() for x in _safe_json_array(raw) if str(x).strip()][:n]
