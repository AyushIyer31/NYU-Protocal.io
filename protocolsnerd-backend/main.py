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
    load_protocol_index,
)
from claude_client import (
    analyze_experiment_request,
    is_available as llm_is_available,
    is_new_search_topic,
    generate_natural_search_queries,
)
from concept_expansion import (
    extract_concepts, expand_concepts, build_search_probes, generate_sentence_variants,
)
from protocolsio_client import multi_probe_search
from protocol_ranker import rank_protocols
from experiment_profile import (
    INTENT_LABELS,
    apply_profile_ranking,
    build_experiment_profile,
    can_generate_search_queries,
    candidate_query_preserves_required_concepts,
    detect_experiment_intent,
    generate_candidate_search_queries,
    merge_profiles,
    needs_clarification,
    next_biology_clarification,
    next_clarification,
    normalize_experiment_goal,
    profile_to_search_query,
    profile_source_query_for_request,
    should_respond_as_chitchat,
    validate_biology_profile,
    _is_generic,
)
from biology_intents import controlled_intent_payload, normalize_sub_intent
from field_ranking import closeness_rank

logging.basicConfig(level=logging.INFO)

app = FastAPI()
update_queues: Dict[str, asyncio.Queue] = {}
main_loop: Optional[asyncio.AbstractEventLoop] = None
executor = ThreadPoolExecutor()

# Protocol RAG index — loaded once at startup
PROTOCOL_INDEX: Optional[Dict[str, Any]] = None
PROTOCOLS_DATA_DIR = Path("../data/protocols")
# Prebuilt index baked into deploy images by scripts/build_index.py.
PROTOCOL_INDEX_CACHE = Path("../data/protocol_index.pkl")

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

    # Prefer a prebuilt index baked into the image (scripts/build_index.py runs
    # at Docker build time). Loading a pickle is fast and CPU-light, so it's safe
    # to do synchronously at startup — the index is ready the moment the server
    # accepts requests. This avoids Cloud Run's between-request CPU throttling,
    # which stalls a runtime build and leaves local search permanently 503-ing.
    if PROTOCOL_INDEX_CACHE.exists():
        try:
            PROTOCOL_INDEX = load_protocol_index(PROTOCOL_INDEX_CACHE)
            logging.info(f"Loaded prebuilt protocol index: {len(PROTOCOL_INDEX['protocols'])} protocols.")
        except Exception as e:
            logging.warning(f"Could not load prebuilt index ({e}); will build at runtime.")

    if PROTOCOL_INDEX is None and PROTOCOLS_DATA_DIR.exists():
        # Local-dev fallback: no prebuilt pickle, so build in a background thread
        # (a dev machine has no CPU throttling, so the build completes fine).
        # Request handlers guard for `PROTOCOL_INDEX is None` until it's ready.
        def _build_index():
            global PROTOCOL_INDEX
            try:
                PROTOCOL_INDEX = build_protocol_index(PROTOCOLS_DATA_DIR)
                logging.info(f"Protocol index ready: {len(PROTOCOL_INDEX['protocols'])} protocols loaded.")
            except Exception as e:
                logging.warning(f"Could not build protocol index: {e}")

        executor.submit(_build_index)
        logging.info("No prebuilt index found; building in background.")
    elif PROTOCOL_INDEX is None:
        logging.warning(f"Protocol data dir not found: {PROTOCOLS_DATA_DIR}. Run fetch_protocols.py first.")

    logging.info("RAG backend started.")


class ChatRequest(BaseModel):
    query: str
    top_k: int = 5
    explain: bool = True
    # Set True when the user is responding to a clarification question,
    # so we don't ask for clarification a second time.
    skip_clarification: bool = False
    # "live"  -> concept-expansion pipeline against the live protocols.io API
    # "local" -> TF-IDF search over the cached protocol index
    search_mode: str = "live"
    # Client-maintained state for the current experimental goal. This lets a
    # short clarification answer like "stable transformation" update the
    # original request instead of replacing it.
    experiment_profile: Optional[Dict[str, Any]] = None
    conversation_query: Optional[str] = None
    # Search only runs after the user chooses/edits generated candidate queries.
    search_confirmed: bool = False
    selected_search_query: Optional[str] = None
    search_all: bool = False
    candidate_search_queries: Optional[List[str]] = None


def run_live_expansion_search(query: str, top_k: int, profile: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
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
    initial_k = max(top_k * 3, top_k)
    ranked = rank_protocols(concepts, expansions, merged, hit_map, top_k=initial_k)
    if profile:
        ranked = apply_profile_ranking(profile, ranked, top_k=top_k)
    else:
        ranked = ranked[:top_k]
    return {
        "results": ranked,
        "concepts": concepts,
        "expansions": expansions,
        "probes": probes,
        "probe_totals": probe_totals,
        "sentence_variants": sentence_variants,
    }


def run_live_candidate_searches(
    queries: List[str],
    top_k: int,
    profile: Optional[Dict[str, Any]] = None,
    raw_query: str = "",
) -> Dict[str, Any]:
    merged: Dict[Any, Dict[str, Any]] = {}
    expanded: List[str] = []
    concepts: Dict[str, Any] = {}
    expansions: Dict[str, List[str]] = {}
    sentence_variants: List[str] = []

    for query in [q for q in queries if q.strip()][:5]:
        live = run_live_expansion_search(query, max(top_k * 2, top_k), profile)
        expanded.extend(live.get("probes", []))
        concepts = concepts or live.get("concepts", {})
        expansions = expansions or live.get("expansions", {})
        sentence_variants.extend(live.get("sentence_variants", []))
        for result in live.get("results", []):
            pid = result.get("id") or result.get("url") or result.get("title")
            if not pid:
                continue
            existing = merged.get(pid)
            if not existing or result.get("profile_score", result.get("score", 0)) > existing.get("profile_score", existing.get("score", 0)):
                merged[pid] = {**result, "selected_query_matches": [query]}
            else:
                existing.setdefault("selected_query_matches", []).append(query)

    results = list(merged.values())
    if profile:
        results = closeness_rank(profile, results, top_k, raw_query=raw_query)
    else:
        results = sorted(results, key=lambda x: x.get("profile_score", x.get("score", 0)), reverse=True)[:top_k]

    return {
        "results": results,
        "expanded": _dedup_strings(expanded),
        "concepts": concepts,
        "expansions": expansions,
        "sentence_variants": _dedup_strings(sentence_variants),
    }


def run_local_candidate_searches(
    queries: List[str],
    top_k: int,
    profile: Dict[str, Any],
    raw_query: str = "",
) -> Dict[str, Any]:
    expanded: List[str] = []
    for query in [q for q in queries if q.strip()][:5]:
        expanded.extend(expand_query(query))
    expanded = _dedup_strings(expanded)
    # Keep a wider candidate pool for the profile-aware ranker. Narrow TF-IDF
    # pools tend to preserve broad "protein/expression" hits before the ranker
    # can reward organism + expression type + tissue matches.
    candidate_pool_size = max(top_k * 24, 120)
    results = multi_search_protocols(PROTOCOL_INDEX, expanded, candidate_pool_size)

    # Organism-merge: the TF-IDF stage forces certain keywords (e.g. "crispr")
    # as required terms, which can penalize an organism-correct protocol out of
    # the pool when it lacks that literal word (e.g. a tomato transformation
    # protocol that doesn't spell out "crispr"). When the user named a SPECIFIC
    # organism, retrieve organism-focused matches directly and merge them in so
    # the profile ranker can evaluate and surface them. Skipped for vague
    # organisms ("plant", "cell") — searching those floods the pool with noise.
    organism = str((profile or {}).get("organism") or "").strip()
    if organism and not _is_generic(organism):
        seen = {r["id"] for r in results}
        organism_queries = _dedup_strings(
            [organism] + [f"{organism} {q}" for q in queries[:3] if q.strip()]
        )
        for oq in organism_queries:
            for r in search_protocols(PROTOCOL_INDEX, oq, top_k=10):
                if r["id"] not in seen:
                    seen.add(r["id"])
                    results.append(r)

    results = closeness_rank(profile, results, top_k, raw_query=raw_query)
    return {
        "results": results,
        "expanded": expanded,
        "concepts": {},
        "expansions": {},
        "sentence_variants": [],
    }


def _dedup_strings(values: List[str]) -> List[str]:
    seen = set()
    out = []
    for value in values:
        key = str(value).strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(str(value).strip())
    return out


_INTENT_ALIASES = {
    "gene modification": "gene_modification",
    "gene_modification": "gene_modification",
    "gene editing": "gene_modification",
    "gene_editing": "gene_modification",
    "genome editing": "gene_modification",
    "genome_editing": "gene_modification",
    "crispr": "gene_modification",
    "gene overexpression": "gene_overexpression",
    "gene_overexpression": "gene_overexpression",
    "overexpression": "gene_overexpression",
    "gene knockdown": "gene_knockdown",
    "gene_knockdown": "gene_knockdown",
    "knockdown": "gene_knockdown",
    "protein purification": "protein_purification",
    "protein_purification": "protein_purification",
    "pcr": "pcr_qpcr",
    "qpcr": "pcr_qpcr",
    "pcr_qpcr": "pcr_qpcr",
    "transformation": "transformation",
    "microscopy": "microscopy",
    "sequencing prep": "sequencing_prep",
    "sequencing_prep": "sequencing_prep",
    "chitchat": "chitchat",
    "unknown": "unknown",
}


def _controlled_intent_name(value: Any, source_query: str = "") -> Optional[str]:
    text = str(value or "").strip().lower().replace("-", " ").replace("_", " ")
    if not text:
        return None
    normalized = normalize_sub_intent(text)
    if normalized != "unknown":
        return normalized
    compact = text.replace(" ", "_")
    if compact in INTENT_LABELS or compact in {"chitchat"}:
        return compact
    if text in _INTENT_ALIASES:
        return _INTENT_ALIASES[text]
    for phrase, intent_name in _INTENT_ALIASES.items():
        if phrase not in {"unknown", "chitchat"} and phrase in text:
            return intent_name
    detected = detect_experiment_intent(" ".join([source_query, text]).strip())
    if detected.get("intent") != "unknown":
        return detected.get("intent")
    return None


def _intent_response(intent_name: str, confidence: Any = 0.7, alternatives: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    if intent_name not in INTENT_LABELS and intent_name != "chitchat":
        intent_name = "unknown"
    return controlled_intent_payload(intent_name, confidence=confidence, alternatives=alternatives)


def _normalize_intent(plan: Dict[str, Any], fallback: Dict[str, Any], source_query: str = "") -> Dict[str, Any]:
    raw = plan.get("intent") if isinstance(plan, dict) else {}
    raw_sub_intent = plan.get("sub_intent") if isinstance(plan, dict) else None
    raw_text = ""
    if raw_sub_intent:
        name = _controlled_intent_name(raw_sub_intent, source_query)
        if (
            name == "gene_overexpression"
            and fallback.get("intent") == "gene_modification"
            and "overexpress" not in source_query.lower()
            and "overexpression" not in source_query.lower()
        ):
            return fallback
        if (not name or name == "unknown") and fallback.get("intent") != "unknown":
            return fallback
        return controlled_intent_payload(
            name or "unknown",
            intent_family=plan.get("intent_family"),
            confidence=plan.get("confidence", 0.7),
        )
    if isinstance(raw, str):
        raw_text = raw
        name = _controlled_intent_name(raw, source_query)
        if (
            name == "gene_overexpression"
            and fallback.get("intent") == "gene_modification"
            and "overexpress" not in source_query.lower()
            and "overexpression" not in source_query.lower()
        ):
            return fallback
        if (not name or name == "unknown") and fallback.get("intent") != "unknown":
            return fallback
        return _intent_response(name or "unknown")
    if isinstance(raw, dict):
        raw_text = " ".join(str(raw.get(key) or "") for key in ("sub_intent", "name", "label"))
        name = _controlled_intent_name(raw_text, source_query)
        if (
            name == "gene_overexpression"
            and fallback.get("intent") == "gene_modification"
            and "overexpress" not in source_query.lower()
            and "overexpression" not in source_query.lower()
        ):
            return fallback
        if (not name or name == "unknown") and fallback.get("intent") != "unknown":
            return fallback
        return controlled_intent_payload(
            name or "unknown",
            intent_family=plan.get("intent_family") or raw.get("intent_family"),
            confidence=raw.get("confidence", 0.7),
            alternatives=raw.get("alternatives", []),
        )
    return fallback


def _clarification_from_plan(plan: Dict[str, Any], profile: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    raw = plan.get("clarifying_question") if isinstance(plan, dict) else None
    if not isinstance(raw, dict):
        return None
    question = str(raw.get("question") or "").strip()
    if not question or question.lower() in {"none", "null"}:
        return None
    field = str(raw.get("field") or "general").strip() or "general"
    if profile and _llm_clarification_is_stale(profile, field, question):
        return None
    options = raw.get("options") if isinstance(raw.get("options"), list) else []
    return {
        "field": field,
        "question": question,
        "options": [str(x) for x in options if str(x).strip()][:6],
    }


def _llm_clarification_is_stale(profile: Dict[str, Any], field: str, question: str) -> bool:
    fields = _clarification_candidate_fields(field, question)
    for candidate in fields:
        if candidate != "general" and not needs_clarification(profile, candidate):
            return True
    return False


def _clarification_candidate_fields(field: str, question: str) -> List[str]:
    fields = []
    if field:
        fields.append(str(field).strip())
    text = str(question or "").strip().lower()
    phrase_fields = [
        ("modification_type", [
            "what type of gene modification",
            "kind of gene modification",
            "modification type",
            "overexpression, knockdown",
            "deletion",
            "insertion",
        ]),
        ("organism", ["organism", "species", "experimental system are you working", "plant species"]),
        ("delivery_method", ["delivery", "delivered", "introduced", "stable transformation", "transient expression"]),
        ("expression_type", ["expression type", "stable whole-plant", "transient expression"]),
        ("readout_assay", ["readout", "assay", "measure", "rna level", "qpcr", "phenotype", "protein level"]),
        ("tissue_or_cell_type", ["tissue", "cell type", "sample type", "where should", "what system"]),
        ("target", ["target", "gene targets", "target class", "protein or tag"]),
        ("condition", ["condition", "treatment", "stress treatment"]),
    ]
    for candidate_field, phrases in phrase_fields:
        if any(phrase in text for phrase in phrases):
            fields.append(candidate_field)
    return _dedup_strings(fields)


def _finalize_candidate_queries(
    queries: List[str],
    original_query: str,
) -> List[str]:
    """Light validation for natural-language query suggestions, and guarantee the
    user's original request is one of the options. Unlike the field-complete
    `candidate_query_preserves_required_concepts`, this allows focused queries
    (each covering a subset of fields) — collective coverage is handled by the
    generator's prompt."""
    out = [
        " ".join(str(q).split())
        for q in (queries or [])
        if _is_valid_candidate_query(q)
    ]
    oq = " ".join(str(original_query or "").split())
    if oq and _is_valid_candidate_query(oq) and oq.lower() not in {x.lower() for x in out}:
        out.append(oq)
    return _dedup_strings(out)[:5]


def _candidate_queries_from_plan(
    plan: Dict[str, Any],
    profile: Dict[str, Any],
    fallback_query: str,
) -> List[str]:
    if not can_generate_search_queries(profile):
        return []
    raw = plan.get("candidate_search_queries") if isinstance(plan, dict) else []
    llm_queries = [
        str(q).strip()
        for q in raw
        if _is_valid_candidate_query(q)
    ] if isinstance(raw, list) else []
    rule_queries = [
        query for query in generate_candidate_search_queries(profile, fallback_query=fallback_query, max_queries=5)
        if _is_valid_candidate_query(query)
    ]
    preserved_llm_queries = [
        query for query in llm_queries
        if candidate_query_preserves_required_concepts(profile, query)
    ]
    if len(preserved_llm_queries) < len(llm_queries):
        return _dedup_strings(rule_queries + preserved_llm_queries)[:5]
    return _dedup_strings(preserved_llm_queries + rule_queries)[:5]


def _is_valid_candidate_query(query: Any) -> bool:
    text = " ".join(str(query or "").split())
    if len(text) < 4:
        return False
    lowered = text.lower()
    if lowered in {"protocol", "protocols", "method", "search"}:
        return False
    if "?" in text:
        return False
    question_starts = (
        "what ",
        "which ",
        "do you ",
        "does ",
        "should ",
        "can you ",
        "are you ",
        "is ",
    )
    return not lowered.startswith(question_starts)


def _profile_goal_summary(profile: Optional[Dict[str, Any]]) -> str:
    """A short natural-language summary of the current search, used as the goal
    for new-topic detection when conversation_query isn't sent by the client."""
    if not profile:
        return ""
    parts = []
    for key in ("sub_intent", "modification_type", "experimental_method",
                "organism", "tissue_or_cell_type", "readout_assay", "condition"):
        v = str(profile.get(key) or "").strip()
        if v and v.lower() not in ("not specified", "none", "unknown", "null"):
            parts.append(v)
    # de-dup while preserving order
    return ", ".join(dict.fromkeys(parts))


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
    conversation_query = (req.conversation_query or "").strip()
    has_active_experiment_context = bool(conversation_query or req.experiment_profile)
    loop = asyncio.get_event_loop()

    # New-topic detection: if the user switches to a clearly different search
    # mid-conversation (e.g. answering a tomato-CRISPR clarification with
    # "western blot in mouse"), clear the carried-over profile so the new search
    # starts clean instead of merging into the old goal. Skipped during the
    # search-confirmation step (clicking a candidate query is not a new topic).
    #
    # The "current goal" comes from conversation_query when present, but falls
    # back to a summary of the carried-over profile — the frontend doesn't always
    # send conversation_query, and detection must not silently skip when it's empty.
    new_search = False
    # Only the confirmation step (clicking a candidate query's "Search") is a
    # search action. Don't gate on selected_search_query / candidate_search_queries:
    # the frontend populates selected_search_query with the typed text on every
    # message, so gating on it would skip new-topic detection for normal input.
    _is_search_action = req.search_confirmed or req.search_all
    _topic_goal = conversation_query or _profile_goal_summary(req.experiment_profile)
    if req.experiment_profile and _topic_goal and not _is_search_action and llm_is_available():
        if await loop.run_in_executor(executor, is_new_search_topic, _topic_goal, query):
            new_search = True
            req.experiment_profile = None
            conversation_query = ""
            has_active_experiment_context = False

    profile_source_query = profile_source_query_for_request(
        query=query,
        conversation_query=conversation_query,
        search_confirmed=req.search_confirmed,
        experiment_profile=req.experiment_profile,
    )
    if not profile_source_query and not (req.search_confirmed and req.experiment_profile):
        profile_source_query = query

    total_indexed = len(PROTOCOL_INDEX["protocols"]) if PROTOCOL_INDEX else 0

    rule_intent = detect_experiment_intent(profile_source_query)
    rule_profile = build_experiment_profile(
        profile_source_query,
        previous_profile=req.experiment_profile,
    )
    llm_plan: Dict[str, Any] = {}
    if not req.search_confirmed and llm_is_available():
        llm_plan = await loop.run_in_executor(
            executor,
            lambda: analyze_experiment_request(
                user_query=query,
                conversation_query=conversation_query,
                previous_profile=req.experiment_profile,
            ),
        )

    experiment_intent = _normalize_intent(llm_plan, rule_intent, profile_source_query)
    if has_active_experiment_context and experiment_intent.get("intent") == "chitchat":
        experiment_intent = rule_intent
    experiment_profile = merge_profiles(
        llm_plan.get("experiment_profile") if llm_plan else None,
        rule_profile,
    )
    experiment_intent, experiment_profile = validate_biology_profile(
        profile_source_query,
        experiment_intent,
        experiment_profile,
    )
    experiment_intent, experiment_profile = normalize_experiment_goal(
        profile_source_query,
        experiment_intent,
        experiment_profile,
    )
    experiment_intent, experiment_profile = validate_biology_profile(
        profile_source_query,
        experiment_intent,
        experiment_profile,
    )
    structured_query = profile_to_search_query(experiment_profile, profile_source_query)
    conversation_query = conversation_query or query

    if not req.search_confirmed:
        next_action = str(llm_plan.get("next_action") or "").strip()
        if should_respond_as_chitchat(has_active_experiment_context, experiment_intent, next_action):
            return {
                "query": query,
                "intent": "chitchat",
                "experiment_intent": experiment_intent,
                "experiment_profile": experiment_profile,
                "new_search": new_search,
                "clarification": None,
                "conversation_query": conversation_query,
                "search_query": structured_query,
                "candidate_search_queries": [],
                "reply": llm_plan.get("reply") or "Describe an experiment and I can help find relevant protocols.",
                "results": [],
                "explanation": "",
                "expanded_queries": [],
                "feedback_prompt": None,
                "feedback_options": [],
                "total_protocols_indexed": total_indexed,
        }

        clarification = None
        if not req.skip_clarification:
            clarification = next_clarification(experiment_profile, experiment_intent)
        if not clarification and next_action == "ask_clarification":
            clarification = _clarification_from_plan(llm_plan, experiment_profile)
        if (
            not clarification
            and not req.skip_clarification
            and not has_active_experiment_context
            and not llm_plan
            and is_vague_query(query)
        ):
            clarification_text = get_clarification_question(query)
            clarification = {
                "field": "general",
                "question": clarification_text,
                "options": [],
            }
        if clarification:
            return {
                "query": query,
                "intent": "clarification",
                "experiment_intent": experiment_intent,
                "experiment_profile": experiment_profile,
                "new_search": new_search,
                "clarification": clarification,
                "conversation_query": conversation_query,
                "search_query": structured_query,
                "candidate_search_queries": [],
                "missing_fields": llm_plan.get("missing_fields", []),
                "reply": clarification["question"],
                "results": [],
                "explanation": "",
                "expanded_queries": [],
                "feedback_prompt": None,
                "feedback_options": [],
                "total_protocols_indexed": total_indexed,
            }

        if not llm_plan and not has_active_experiment_context:
            intent = await loop.run_in_executor(executor, classify_intent, query)
            if intent["intent"] == "chitchat":
                return {
                    "query": query,
                    "intent": "chitchat",
                    "experiment_intent": experiment_intent,
                    "experiment_profile": experiment_profile,
                "new_search": new_search,
                    "clarification": None,
                    "conversation_query": conversation_query,
                    "search_query": structured_query,
                    "candidate_search_queries": [],
                    "reply": intent["reply"],
                    "results": [],
                    "explanation": "",
                    "expanded_queries": [],
                    "feedback_prompt": None,
                    "feedback_options": [],
                    "total_protocols_indexed": total_indexed,
                }

        # Prefer Claude-generated natural-language suggestions (focused angles
        # that collectively cover every field + include the original query).
        # Fall back to the rule-based generator when Claude is unavailable.
        candidate_queries: List[str] = []
        if llm_is_available() and can_generate_search_queries(experiment_profile):
            nl_queries = await loop.run_in_executor(
                executor,
                generate_natural_search_queries,
                experiment_profile,
                conversation_query or query,
                5,
            )
            candidate_queries = _finalize_candidate_queries(nl_queries, conversation_query or query)
        if not candidate_queries:
            candidate_queries = _candidate_queries_from_plan(
                llm_plan,
                experiment_profile,
                structured_query,
            )
        if not candidate_queries:
            clarification = (
                next_clarification(experiment_profile, experiment_intent)
                or _clarification_from_plan(llm_plan, experiment_profile)
            )
            if not clarification:
                clarification = {
                    "field": "experimental_method",
                    "question": "What experimental task are you trying to run?",
                    "options": [
                        "gene overexpression",
                        "gene knockdown",
                        "PCR/qPCR",
                        "protein purification",
                        "microscopy",
                    ],
                }
            return {
                "query": query,
                "intent": "clarification",
                "experiment_intent": experiment_intent,
                "experiment_profile": experiment_profile,
                "new_search": new_search,
                "clarification": clarification,
                "conversation_query": conversation_query,
                "search_query": structured_query,
                "candidate_search_queries": [],
                "missing_fields": llm_plan.get("missing_fields", []),
                "reply": clarification["question"],
                "results": [],
                "explanation": "",
                "expanded_queries": [],
                "feedback_prompt": None,
                "feedback_options": [],
                "total_protocols_indexed": total_indexed,
            }
        return {
            "query": query,
            "intent": "query_selection",
            "experiment_intent": experiment_intent,
            "experiment_profile": experiment_profile,
            "new_search": new_search,
            "clarification": None,
            "conversation_query": conversation_query,
            "search_query": structured_query,
            "candidate_search_queries": candidate_queries,
            "missing_fields": llm_plan.get("missing_fields", []),
            "reply": "Choose a search query to run, edit one, or search all suggested queries.",
            "results": [],
            "explanation": "",
            "expanded_queries": [],
            "feedback_prompt": None,
            "feedback_options": [],
            "total_protocols_indexed": total_indexed,
        }

    selected_query = (req.selected_search_query or query).strip()
    candidate_queries = [q for q in (req.candidate_search_queries or []) if str(q).strip()]
    search_queries = candidate_queries if req.search_all and candidate_queries else [selected_query]
    search_queries = _dedup_strings([str(q) for q in search_queries if str(q).strip()])
    if not search_queries:
        search_queries = [structured_query]
    structured_query = " | ".join(search_queries) if req.search_all else search_queries[0]

    # Confirmed search. Either the live concept-expansion pipeline (default) or
    # the legacy local TF-IDF pipeline.
    # The user's natural-language request is used only for "like/such as"
    # relaxation detection in the closeness ranker — not for retrieval.
    like_query = " ".join(filter(None, [conversation_query, query])).strip()
    concepts: Dict[str, Any] = {}
    expansions: Dict[str, List[str]] = {}
    sentence_variants: List[str] = []
    if req.search_mode == "live":
        try:
            live = await loop.run_in_executor(
                executor,
                run_live_candidate_searches,
                search_queries,
                req.top_k,
                experiment_profile,
                like_query,
            )
            results = live["results"]
            concepts = live["concepts"]
            expansions = live["expansions"]
            expanded = live["expanded"]
            sentence_variants = live["sentence_variants"]
            logging.info(f"Live confirmed search '{structured_query}': {len(expanded)} probes -> {len(results)} ranked results")
        except Exception as e:
            logging.warning(f"Live search failed ({e}); falling back to local index.")
            if not PROTOCOL_INDEX:
                raise HTTPException(status_code=503, detail=f"Live search failed and no local index: {e}")
            local = await loop.run_in_executor(
                executor,
                run_local_candidate_searches,
                search_queries,
                req.top_k,
                experiment_profile,
                like_query,
            )
            results = local["results"]
            expanded = local["expanded"]
    else:
        local = await loop.run_in_executor(
            executor,
            run_local_candidate_searches,
            search_queries,
            req.top_k,
            experiment_profile,
            like_query,
        )
        results = local["results"]
        expanded = local["expanded"]
        logging.info(f"Local confirmed search '{structured_query}' expanded into {len(expanded)} queries")

    # Step 5: Optionally explain the top results using the local LLM.
    # Skip entirely when Ollama is unreachable — otherwise explain_matches would
    # spend ~15s on doomed retries and stall the request.
    explanation = ""
    if req.explain and results and llm_is_available():
        explanation = await loop.run_in_executor(
            executor, explain_matches, structured_query, results[:3]
        )

    return {
        "query": query,
        "intent": "search",
        "experiment_intent": experiment_intent,
        "experiment_profile": experiment_profile,
        "new_search": new_search,
        "clarification": None,
        "conversation_query": conversation_query,
        "search_query": structured_query,
        "candidate_search_queries": candidate_queries,
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


# Serve the static frontend from the same origin for single-container deploys
# (Cloud Run, HF Spaces, Docker). All API routes above are registered first and
# take precedence; any other path falls through to the static files. The chat
# UI is at /chat.html. Harmless locally and on the Render split deploy (the
# frontend is a separate service there, but mounting its files here is fine).
from fastapi.staticfiles import StaticFiles
from pathlib import Path as _Path

_FRONTEND_DIR = _Path(__file__).resolve().parent.parent / "protocolsnerd-website"
if _FRONTEND_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(_FRONTEND_DIR), html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
