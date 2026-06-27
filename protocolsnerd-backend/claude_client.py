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
    _get_openai_model,
    _get_gemini_model,
    _retryable_ollama_call,
    active_provider,
    claude_available,
    gemini_available,
)

load_dotenv(Path(__file__).parent / "variables.env", override=False)

log = logging.getLogger(__name__)

# Global provider override for the current request (set by main.py chat endpoint)
_current_provider: Optional[str] = None

def set_provider(provider: Optional[str]) -> None:
    """Set the LLM provider for this request. Call before LLM operations."""
    global _current_provider
    _current_provider = provider

def get_provider() -> Optional[str]:
    """Get the current provider override."""
    return _current_provider


def _native_ollama_base_url() -> str:
    base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").strip('"').strip()
    if not base_url:
        base_url = "http://localhost:11434"
    base_url = base_url.rstrip("/")
    if base_url.endswith("/v1"):
        base_url = base_url[:-3].rstrip("/")
    return base_url


def active_model() -> str:
    """Model id for the resolved provider (respects the per-request override)."""
    prov = active_provider(override=get_provider())
    if prov == "openai":
        return _get_openai_model()
    if prov == "claude":
        return _get_claude_model()
    if prov == "gemini":
        return _get_gemini_model()
    return _get_ollama_model()


def current_llm_info() -> Dict[str, Any]:
    """Provider, model id, and availability for the current request — surfaced in
    the /chat response so the UI can show which model produced the results."""
    prov = active_provider(override=get_provider())
    return {
        "provider": prov,
        "model": active_model(),
        "available": is_available(),
    }


def is_available(provider: Optional[str] = None) -> bool:
    """True when the active LLM provider is configured and reachable."""
    from ollama_executions import openai_available
    resolved_provider = provider or get_provider()
    resolved = active_provider(override=resolved_provider)
    if resolved == "openai":
        return openai_available()
    if resolved == "claude":
        return claude_available()
    if resolved == "gemini":
        return gemini_available()
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
    provider: Optional[str] = None,
) -> str:
    """LLM call (Ollama/Claude/OpenAI). Returns empty string on failure."""
    resolved_provider = provider or get_provider()
    if not is_available(provider=resolved_provider):
        return ""
    raw = _retryable_ollama_call(
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=temperature,
        response_format=response_format,
        provider=resolved_provider,
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
    pending_field: Optional[str] = None,
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
        "user_declined_to_answer": False,
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
            "Answering a specific question:\n"
            "- When 'pending_field' is set, the current_user_message is the ANSWER to that "
            "specific field. Assign the answer to THAT field — do not place it in a different "
            "field. E.g. if pending_field is 'readout_assay' and the user says 'phenotype', set "
            "readout_assay='phenotype' (NOT organism). If the message clearly also specifies "
            "other fields, you may fill those too, but the pending_field is the primary target.\n\n"
            "Decline detection:\n"
            "- Set user_declined_to_answer=true ONLY when the user's latest message is the "
            "answer to your pending clarification AND it declines to specify (e.g. 'not sure', "
            "'I don't know', 'no clue', 'no preference', 'you choose', 'whatever', 'doesn't "
            "matter', 'skip it', 'either is fine'). For any concrete answer, set it false.\n\n"
            "Field-value rules:\n"
            "- PRESERVE conditional operators VERBATIM in field values. If the user writes "
            "'rice or tomato', store the field as 'rice or tomato' (do NOT pick one or "
            "generalize to 'plant'). Likewise keep 'X and Y', 'like X', 'similar to X', and "
            "'such as X' exactly as written in the relevant field.\n\n"
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
                "pending_field": pending_field or None,
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


def is_new_search_topic(current_goal: str, new_message: str) -> bool:
    """
    True when the new message starts a clearly different search rather than
    answering or refining the current one. Used to reset the experiment profile
    mid-conversation when the user switches topics.

    Conservative: returns False (stay in the current search) whenever the LLM is
    unavailable or unsure — it never resets on doubt, so a clarification answer
    like "banana" or "stable transformation" is never mistaken for a new search.
    """
    current_goal = (current_goal or "").strip()
    new_message = (new_message or "").strip()
    if not current_goal or not new_message or not is_available():
        return False
    raw = _call(
        system=(
            "A scientist is building ONE protocol-search request over a conversation. "
            "Given their CURRENT search goal and their NEW message, decide whether the new "
            "message belongs to the SAME search or starts a NEW one. Answer with EXACTLY one "
            "word: SAME or NEW.\n"
            "SAME — the message answers the pending question, or briefly refines ONE detail "
            "of the current search (e.g. 'banana', 'use mouse instead', 'how about rice', "
            "'stable transformation', 'leaf tissue', 'look at qPCR'). A short tweak that keeps "
            "the same overall experiment is SAME, even if it changes the organism.\n"
            "NEW — the message is a COMPLETE, standalone protocol-search request (e.g. it "
            "starts like 'Find protocols for ...' / 'I want protocols that ...' and specifies "
            "an experiment), OR it switches to a different technique/experiment type (e.g. "
            "genome editing -> western blot, or -> a drought-tolerance assay). A fully "
            "re-stated request is NEW even if its technique overlaps the current one.\n"
            "When unsure, answer SAME."
        ),
        user=f"CURRENT GOAL: {current_goal}\nNEW MESSAGE: {new_message}",
        max_tokens=8,
        temperature=0.0,
    )
    return raw.strip().upper().startswith("NEW")


def generate_natural_search_queries(
    profile: Dict[str, Any],
    original_query: str,
    n: int = 5,
) -> List[str]:
    """
    Generate natural-language protocol-search queries from the structured
    profile + the user's original request.

    Each query is a focused angle; across the set every populated field value is
    used at least once (collective coverage); the original query is included; and
    only concepts from the profile/original request are used (no invented terms
    like "in planta" for a non-plant). Returns [] when the LLM is unavailable.
    """
    if not is_available():
        return []

    p = profile or {}
    field_labels = [
        ("organism", p.get("organism")),
        ("system / sample type", p.get("tissue_or_cell_type") or p.get("sample_type")),
        ("target", p.get("target")),
        ("modification / technique", p.get("modification_type")),
        ("method / approach", p.get("sub_intent") or p.get("experimental_method")),
        ("delivery method", p.get("delivery_method")),
        ("expression type", p.get("expression_type")),
        ("readout", p.get("readout_assay") or p.get("readout")),
        ("condition", p.get("condition")),
    ]
    skip = {"", "not specified", "none", "unknown", "not sure", "null"}
    fields = [
        f"- {label}: {str(val).strip()}"
        for label, val in field_labels
        if str(val or "").strip().lower() not in skip
    ]
    intent_specific = p.get("intent_specific")
    if isinstance(intent_specific, dict) and intent_specific:
        extras = "; ".join(
            f"{k}: {v}" for k, v in intent_specific.items()
            if v not in (None, "", False) and str(v).strip().lower() not in skip
        )
        if extras:
            fields.append(f"- additional details: {extras}")
    fields_block = "\n".join(fields)

    raw = _call(
        system=(
            "You write search queries for a biology protocol database (protocols.io). "
            f"Given a structured experiment profile and the scientist's original request, "
            f"write {n} search queries as NATURAL-LANGUAGE phrases.\n"
            "Rules:\n"
            "- Each query is a short, readable phrase — not a keyword dump and not a question.\n"
            "- Each query focuses on a DIFFERENT combination of the fields (a different angle).\n"
            "- ACROSS the whole set, use EVERY field value at least once — don't ignore any field.\n"
            "- Vary phrasing with natural synonyms (scientific organism names, 'simultaneous' for "
            "multiplex, 'western blot' for protein level, etc.).\n"
            "- Include the scientist's ORIGINAL request as one of the queries, essentially unchanged.\n"
            "- PRESERVE conditional operators VERBATIM. If a field value contains 'or' "
            "('rice or tomato'), 'and' ('rice and maize'), or 'like'/'similar to'/'such as' "
            "('like tomato'), keep that exact phrasing in the queries — do NOT pick one side, "
            "drop the operator, or rephrase it (e.g. write '...in rice or tomato...').\n"
            "- Use ONLY concepts present in the profile or the original request. NEVER invent "
            "organisms, tissues, genes, or techniques. In particular, never write 'in planta' "
            "unless the system is literally a plant.\n"
            "Return ONLY a JSON array of strings, no explanation."
        ),
        user=f"ORIGINAL REQUEST: {original_query}\n\nEXPERIMENT PROFILE:\n{fields_block}",
        max_tokens=400,
        temperature=0.3,
    )
    return [str(x).strip() for x in _safe_json_array(raw) if str(x).strip()][:n]
