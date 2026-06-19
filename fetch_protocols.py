#!/usr/bin/env python3
"""
Fetch and cache public protocols from protocols.io into data/protocols/.

Each protocol is saved as data/protocols/<id>.json.
A combined index is written to data/protocols_index.json.

Usage:
    python fetch_protocols.py
    python fetch_protocols.py --keywords "RNA extraction" "PCR" "western blot"
    python fetch_protocols.py --max-per-keyword 50 --output-dir data/protocols

Requires PROTOCOLS_IO_TOKEN in customnerd-backend/variables.env (or as env var).
Get a free CLIENT_ACCESS_TOKEN at: https://www.protocols.io/developers
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import urllib.request
import urllib.parse
import urllib.error

from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# Load token from variables.env
load_dotenv(Path(__file__).parent / "customnerd-backend" / "variables.env", override=False)

API_BASE = "https://www.protocols.io/api/v3"
RATE_LIMIT_DELAY = 0.65  # ~92 req/min, safely under the 100/min limit

# Comprehensive biology keyword list — covers all major protocol categories
DEFAULT_KEYWORDS = [
    # -----------------------------------------------------------------------
    # CRITICAL: Prof. Shasha's 3 benchmark query topics (currently under-covered)
    # -----------------------------------------------------------------------
    "drought tolerance",
    "drought stress",
    "drought tolerance rice",
    "drought stress plant",
    "drought stress Arabidopsis",
    "osmotic stress tolerance",
    "water deficit plant",
    "dehydration tolerance",
    "salt stress tolerance",
    "heat stress plant",
    "abiotic stress tolerance",
    "rice drought",
    "rice stress",
    "Oryza sativa stress",
    "Oryza sativa transformation",
    "rice transformation",
    "rice CRISPR",
    "rice gene editing",
    "rice RNA extraction",
    "rice protein extraction",
    "rice phenotyping",
    "rice seed germination",
    "rice callus",
    "agrobacterium infiltration",
    "agrobacterium transformation plant",
    "agroinfiltration",
    "in planta transformation",
    "floral dip transformation",
    "vacuum infiltration plant",
    "transient expression plant",
    "in planta gene editing",
    "multiplex CRISPR",
    "multiplex gene editing",
    "multiplex genome editing",
    "combinatorial CRISPR",
    "simultaneous gene knockout",
    "multiple gene knockout",
    "multiplex knockout",
    "CRISPR multiplex mouse",
    "multiplex transcription factor",
    "transcription factor binding",
    "transcription factor mouse",
    "transcription factor plant",
    "gene regulation mouse",
    # -----------------------------------------------------------------------
    # RNA — molecular biology core
    # -----------------------------------------------------------------------
    "RNA extraction",
    "total RNA extraction",
    "RNA isolation",
    "RNA isolation plant",
    "RNA isolation tissue",
    "RNA isolation bacteria",
    "RNA isolation yeast",
    "RNA isolation blood",
    "RNA isolation FFPE",
    "mRNA isolation",
    "mRNA extraction",
    "small RNA isolation",
    "microRNA extraction",
    "RNA purification",
    "RNA quality assessment",
    "TRIzol RNA extraction",
    "CTAB RNA extraction",
    "RNeasy RNA extraction",
    "RNA-seq library preparation",
    "single cell RNA sequencing",
    "bulk RNA sequencing",
    "cDNA synthesis",
    "reverse transcription",
    "RT-PCR",
    "quantitative RT-PCR",
    # -----------------------------------------------------------------------
    # DNA
    # -----------------------------------------------------------------------
    "DNA extraction",
    "genomic DNA extraction",
    "DNA isolation plant",
    "DNA isolation tissue",
    "DNA isolation blood",
    "DNA isolation bacteria",
    "CTAB DNA extraction",
    "phenol chloroform extraction",
    "plasmid extraction",
    "plasmid purification",
    "miniprep",
    "maxiprep",
    "DNA gel electrophoresis",
    "agarose gel electrophoresis",
    "DNA quantification",
    "DNA library preparation",
    "bisulfite sequencing",
    "whole genome sequencing",
    "ChIP-seq",
    "ATAC-seq",
    "Hi-C",
    # -----------------------------------------------------------------------
    # PCR variants
    # -----------------------------------------------------------------------
    "PCR amplification",
    "qPCR",
    "quantitative PCR",
    "colony PCR",
    "site-directed mutagenesis",
    "overlap extension PCR",
    "digital PCR",
    "droplet digital PCR",
    "long range PCR",
    # -----------------------------------------------------------------------
    # CRISPR and gene editing
    # -----------------------------------------------------------------------
    "CRISPR Cas9",
    "CRISPR Cas12a",
    "CRISPR knockout",
    "CRISPR knockin",
    "CRISPR guide RNA",
    "guide RNA design",
    "base editing",
    "prime editing",
    "CRISPRa",
    "CRISPRi",
    "CRISPR screen",
    "CRISPR mammalian cells",
    "CRISPR plant",
    "CRISPR zebrafish",
    "CRISPR mouse",
    "homology directed repair",
    "HDR template",
    "electroporation CRISPR",
    # -----------------------------------------------------------------------
    # Protein work
    # -----------------------------------------------------------------------
    "protein extraction",
    "protein extraction plant",
    "protein extraction tissue",
    "protein purification",
    "recombinant protein expression",
    "protein expression E. coli",
    "protein expression yeast",
    "His-tag purification",
    "GST purification",
    "affinity chromatography",
    "size exclusion chromatography",
    "gel filtration",
    "protein concentration",
    "western blot",
    "western blotting",
    "SDS-PAGE",
    "2D gel electrophoresis",
    "co-immunoprecipitation",
    "immunoprecipitation",
    "pull-down assay",
    "protein interaction",
    "ELISA",
    "sandwich ELISA",
    "protein quantification",
    "Bradford assay",
    "BCA assay",
    "mass spectrometry proteomics",
    "proteomics sample preparation",
    # -----------------------------------------------------------------------
    # Cell biology
    # -----------------------------------------------------------------------
    "cell culture",
    "mammalian cell culture",
    "primary cell culture",
    "cell line maintenance",
    "cell passaging",
    "cell counting",
    "cell viability",
    "MTT assay",
    "cell proliferation assay",
    "apoptosis assay",
    "flow cytometry",
    "FACS sorting",
    "cell cycle analysis",
    "transfection",
    "lipofection",
    "calcium phosphate transfection",
    "electroporation",
    "lentiviral transduction",
    "adeno-associated virus",
    "retroviral transduction",
    "stable cell line",
    "cell freezing",
    "cryopreservation",
    "thawing cells",
    # -----------------------------------------------------------------------
    # Microscopy and imaging
    # -----------------------------------------------------------------------
    "immunofluorescence",
    "immunohistochemistry",
    "confocal microscopy",
    "fluorescence microscopy",
    "live cell imaging",
    "super resolution microscopy",
    "TIRF microscopy",
    "electron microscopy",
    "transmission electron microscopy",
    "scanning electron microscopy",
    "cryo-EM",
    "cryo-EM sample preparation",
    "calcium imaging",
    "GFP imaging",
    # -----------------------------------------------------------------------
    # Cloning and molecular tools
    # -----------------------------------------------------------------------
    "molecular cloning",
    "restriction cloning",
    "Gibson assembly",
    "Golden Gate cloning",
    "Gateway cloning",
    "ligation",
    "restriction digest",
    "bacterial transformation",
    "competent cells",
    "E. coli expression",
    "yeast transformation",
    "yeast two-hybrid",
    "two-hybrid assay",
    "reporter assay",
    "luciferase assay",
    # -----------------------------------------------------------------------
    # Plant biology — comprehensive
    # -----------------------------------------------------------------------
    "plant transformation",
    "plant cell culture",
    "plant tissue culture",
    "plant regeneration",
    "callus induction",
    "shoot regeneration",
    "Arabidopsis",
    "Arabidopsis thaliana",
    "Arabidopsis transformation",
    "Arabidopsis RNA",
    "Arabidopsis protein",
    "Arabidopsis CRISPR",
    "Arabidopsis phenotyping",
    "Arabidopsis seedling",
    "Nicotiana benthamiana",
    "tobacco transformation",
    "maize transformation",
    "wheat transformation",
    "soybean transformation",
    "tomato transformation",
    "potato transformation",
    "barley transformation",
    "plant RNA extraction",
    "plant protein extraction",
    "plant DNA extraction",
    "plant phenotyping",
    "plant hormone",
    "auxin",
    "cytokinin",
    "gibberellin",
    "plant pathogen",
    "plant immunity",
    "plant defense",
    "chloroplast isolation",
    "chloroplast transformation",
    "mitochondria isolation plant",
    "plant protoplast",
    "plant cell wall",
    "stomata measurement",
    "photosynthesis measurement",
    "chlorophyll extraction",
    "seed germination",
    "root growth",
    "plant hormone measurement",
    # -----------------------------------------------------------------------
    # Model organisms
    # -----------------------------------------------------------------------
    "zebrafish protocol",
    "zebrafish embryo",
    "zebrafish CRISPR",
    "zebrafish injection",
    "Drosophila protocol",
    "Drosophila CRISPR",
    "Drosophila genetics",
    "C. elegans protocol",
    "C. elegans CRISPR",
    "C. elegans genetics",
    "mouse protocol",
    "mouse dissection",
    "mouse tissue collection",
    "mouse genotyping",
    "mouse behavior",
    "mouse knockout",
    "rat protocol",
    "yeast Saccharomyces",
    "yeast genetics",
    "yeast protein expression",
    # -----------------------------------------------------------------------
    # Sequencing and genomics
    # -----------------------------------------------------------------------
    "next generation sequencing",
    "Illumina sequencing",
    "Oxford Nanopore sequencing",
    "PacBio sequencing",
    "library preparation sequencing",
    "adapter ligation",
    "ChIP-seq library",
    "ATAC-seq library",
    "CUT&RUN",
    "CUT&TAG",
    "spatial transcriptomics",
    "single cell sequencing",
    "10X Genomics",
    "single nucleus RNA",
    "Sanger sequencing",
    "genome assembly",
    # -----------------------------------------------------------------------
    # Neuroscience
    # -----------------------------------------------------------------------
    "neuron culture",
    "primary neuron culture",
    "brain slice",
    "patch clamp",
    "electrophysiology",
    "brain tissue dissection",
    "synaptic protein",
    "neural differentiation",
    "iPSC neuron",
    "calcium imaging neuron",
    # -----------------------------------------------------------------------
    # Immunology
    # -----------------------------------------------------------------------
    "antibody production",
    "antibody purification",
    "ELISA cytokine",
    "cytokine measurement",
    "T cell isolation",
    "B cell isolation",
    "PBMC isolation",
    "dendritic cell",
    "macrophage culture",
    "neutrophil isolation",
    "NK cell",
    "immune cell",
    "flow cytometry immune",
    "intracellular staining",
    # -----------------------------------------------------------------------
    # Biochemistry
    # -----------------------------------------------------------------------
    "enzyme activity assay",
    "kinase assay",
    "phosphorylation assay",
    "ubiquitination assay",
    "FRET assay",
    "surface plasmon resonance",
    "isothermal titration calorimetry",
    "electrophoretic mobility shift",
    "EMSA",
    "chromatin immunoprecipitation",
    "metabolite extraction",
    "lipid extraction",
    "metabolomics",
    "lipidomics",
    "GC-MS metabolomics",
    "LC-MS proteomics",
    # -----------------------------------------------------------------------
    # Stem cells and development
    # -----------------------------------------------------------------------
    "iPSC reprogramming",
    "iPSC differentiation",
    "embryoid body",
    "organoid culture",
    "brain organoid",
    "intestinal organoid",
    "stem cell culture",
    "hematopoietic stem cell",
    "mesenchymal stem cell",
    "cardiac differentiation",
    # -----------------------------------------------------------------------
    # Microbiology
    # -----------------------------------------------------------------------
    "bacterial culture",
    "bacterial growth",
    "biofilm formation",
    "minimal inhibitory concentration",
    "antibiotic susceptibility",
    "16S rRNA sequencing",
    "microbiome analysis",
    "phage transduction",
    "bacterial genetics",
    "gram staining",
    # -----------------------------------------------------------------------
    # Structural biology
    # -----------------------------------------------------------------------
    "protein crystallization",
    "X-ray crystallography",
    "NMR spectroscopy protein",
    "negative stain electron microscopy",
    "cryo-EM grid preparation",
    "protein structure",
    # -----------------------------------------------------------------------
    # Drug discovery and toxicology
    # -----------------------------------------------------------------------
    "IC50 assay",
    "cytotoxicity assay",
    "drug treatment",
    "drug screening",
    "high throughput screening",
    "cell-based assay",
    "genotoxicity assay",
    # -----------------------------------------------------------------------
    # Clinical and diagnostic
    # -----------------------------------------------------------------------
    "blood collection",
    "plasma isolation",
    "serum preparation",
    "tissue biopsy",
    "FFPE tissue",
    "immunohistochemistry tissue",
    "clinical sample",
    "biobank protocol",
    # -----------------------------------------------------------------------
    # Histology and tissue processing
    # -----------------------------------------------------------------------
    "tissue fixation",
    "paraffin embedding",
    "cryosectioning",
    "H&E staining",
    "tissue sectioning",
    "histology staining",
    "antigen retrieval",
]


def _get_token_from_client_credentials(client_id: str, client_secret: str) -> str:
    """
    Exchange client_id + client_secret for a CLIENT_ACCESS_TOKEN.
    This only needs to be done once — the token is then saved to variables.env.
    """
    url = "https://www.protocols.io/api/v3/oauth/token"
    payload = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "client_credentials",
    }).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": BROWSER_UA,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            token = body.get("access_token") or body.get("client_access_token") or ""
            if not token:
                raise SystemExit(f"[ERROR] Token exchange succeeded but no token in response: {body}")
            log.info("Token obtained successfully.")
            return token
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        raise SystemExit(f"[ERROR] Token exchange failed (HTTP {e.code}): {body}")


def _save_token_to_env(token: str):
    """Write the obtained token back into variables.env so future runs skip this step."""
    env_path = Path(__file__).parent / "customnerd-backend" / "variables.env"
    text = env_path.read_text(encoding="utf-8")
    if "PROTOCOLS_IO_TOKEN=" in text:
        lines = []
        for line in text.splitlines():
            if line.startswith("PROTOCOLS_IO_TOKEN="):
                lines.append(f"PROTOCOLS_IO_TOKEN={token}")
            else:
                lines.append(line)
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    else:
        env_path.write_text(text.rstrip() + f"\nPROTOCOLS_IO_TOKEN={token}\n", encoding="utf-8")
    log.info(f"Token saved to {env_path}")


def _get_token() -> str:
    token = os.getenv("PROTOCOLS_IO_TOKEN", "").strip().strip('"')
    if token:
        return token

    # Fall back: check for client_id / client_secret
    client_id = os.getenv("PROTOCOLS_IO_CLIENT_ID", "").strip().strip('"')
    client_secret = os.getenv("PROTOCOLS_IO_CLIENT_SECRET", "").strip().strip('"')

    if client_id and client_secret:
        log.info("No token found — exchanging client_id/client_secret for access token...")
        token = _get_token_from_client_credentials(client_id, client_secret)
        _save_token_to_env(token)
        return token

    raise SystemExit(
        "\n[ERROR] No credentials found. Do one of the following:\n\n"
        "  OPTION A — paste your client_id and client_secret (from protocols.io/developers):\n"
        "    Add to customnerd-backend/variables.env:\n"
        "      PROTOCOLS_IO_CLIENT_ID=your_client_id\n"
        "      PROTOCOLS_IO_CLIENT_SECRET=your_client_secret\n"
        "    Then re-run. The script will fetch and save the token automatically.\n\n"
        "  OPTION B — paste the token directly if you already have it:\n"
        "    Add to customnerd-backend/variables.env:\n"
        "      PROTOCOLS_IO_TOKEN=your_token_here\n"
    )


BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def _api_get(path: str, params: Dict[str, Any], token: str) -> Dict[str, Any]:
    """Make a single authenticated GET request to the protocols.io API."""
    url = f"{API_BASE}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": BROWSER_UA,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
            return json.loads(body)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        log.warning(f"HTTP {e.code} for {url}: {body[:200]}")
        return {}
    except Exception as e:
        log.warning(f"Request failed for {url}: {e}")
        return {}


def _fetch_protocols_page(
    keyword: str,
    page_id: int,
    page_size: int,
    token: str,
) -> tuple[List[Dict], bool]:
    """
    Fetch one page of protocols for a keyword.
    Returns (list_of_protocols, has_more_pages).
    """
    params = {
        "filter": "public",
        "key": keyword,
        "order_field": "activity",
        "order_dir": "desc",
        "page_id": page_id,
        "page_size": page_size,
    }
    data = _api_get("/protocols", params, token)

    if not data or data.get("status_code") not in (0, None):
        log.warning(f"API error for keyword='{keyword}' page={page_id}: {data.get('status_message', 'unknown')}")
        return [], False

    items = data.get("items", []) or []
    pagination = data.get("pagination", {}) or {}
    total_pages = pagination.get("total_pages", 1)
    has_more = page_id < total_pages

    return items, has_more


def _strip_html(text: str) -> str:
    """Remove HTML tags from a string."""
    import re
    if not text:
        return ""
    return re.sub(r"<[^>]+>", " ", str(text)).strip()


def _fetch_full_protocol(protocol_id: int, token: str) -> Optional[Dict[str, Any]]:
    """
    Fetch a single protocol by ID to get its full step data.
    The list endpoint omits steps; this individual endpoint includes them.
    """
    data = _api_get(f"/protocols/{protocol_id}", {}, token)
    # The individual-protocol response nests the protocol under the top-level
    # 'protocol' key (NOT 'payload').
    return data.get("protocol") or (data if data.get("id") else None)


def _extract_steps(raw: Dict[str, Any]) -> List[str]:
    """
    Extract readable plain-text steps from a full protocol object.

    Prefers the step-level `step` field (the instruction HTML). Falls back to
    description components, skipping type_id=6 section headers.
    """
    steps = []
    for s in (raw.get("steps") or []):
        text = _strip_html(s.get("step") or "")
        if not text:
            parts = []
            for comp in (s.get("components") or []):
                if comp.get("type_id") == 6:  # section header, not an instruction
                    continue
                source = comp.get("source") or {}
                t = _strip_html(
                    source.get("description")
                    or source.get("body")
                    or source.get("title")
                    or ""
                )
                if t and t.lower() not in {"note", "warning", "tip"}:
                    parts.append(t)
            text = " ".join(parts).strip()
        if text:
            steps.append(text)
    return steps


def _extract_protocol_fields(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Pull out the fields most useful for RAG indexing."""

    steps = _extract_steps(raw)

    # Authors
    authors_raw = raw.get("authors") or []
    authors = [
        a.get("name") or f"{a.get('fname', '')} {a.get('lname', '')}".strip()
        for a in authors_raw
        if isinstance(a, dict)
    ]

    creator = raw.get("creator") or {}
    if isinstance(creator, dict):
        creator_name = creator.get("name") or f"{creator.get('fname', '')} {creator.get('lname', '')}".strip()
    else:
        creator_name = ""

    return {
        "id": raw.get("id"),
        "title": raw.get("title") or "",
        "uri": raw.get("uri") or "",
        "doi": raw.get("doi") or "",
        "description": raw.get("description") or "",
        "guidelines": raw.get("guidelines") or "",
        "before_start": raw.get("before_start") or "",
        "warning": raw.get("warning") or "",
        "materials_text": raw.get("materials_text") or "",
        "steps": steps,
        "authors": [a for a in authors if a],
        "creator": creator_name,
        "published_on": raw.get("published_on") or "",
        "created_on": raw.get("created_on") or "",
        "keywords": [
            kw.get("name") or kw if isinstance(kw, str) else ""
            for kw in (raw.get("keywords") or [])
        ],
    }


def fetch_and_cache(
    keywords: List[str],
    output_dir: Path,
    max_per_keyword: int,
    token: str,
    skip_steps: bool = False,
) -> List[Dict[str, Any]]:
    """
    For each keyword, paginate through protocols.io and save each protocol to disk.
    Deduplicates across keywords by protocol ID.
    Returns the full list of saved protocol metadata (for the index).
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    seen_ids: Set[int] = set()
    index_entries: List[Dict[str, Any]] = []

    # Resume: load already-fetched IDs from existing index
    index_path = output_dir.parent / "protocols_index.json"
    if index_path.exists():
        try:
            existing = json.loads(index_path.read_text(encoding="utf-8"))
            for entry in existing:
                seen_ids.add(entry["id"])
                index_entries.append(entry)
            log.info(f"Resuming — {len(seen_ids)} protocols already cached.")
        except Exception:
            pass

    page_size = min(50, max_per_keyword)
    total_new = 0

    for keyword in keywords:
        log.info(f"--- Fetching keyword: '{keyword}' (max {max_per_keyword}) ---")
        fetched_this_kw = 0
        page_id = 0  # protocols.io pagination is 0-indexed; page_id=0 is the
                     # first (most relevant) page. Starting at 1 skips it.

        while fetched_this_kw < max_per_keyword:
            items, has_more = _fetch_protocols_page(keyword, page_id, page_size, token)
            time.sleep(RATE_LIMIT_DELAY)

            if not items:
                break

            for raw in items:
                pid = raw.get("id")
                if not pid or pid in seen_ids:
                    continue
                if fetched_this_kw >= max_per_keyword:
                    break

                # Optionally fetch full protocol to get steps (doubles API calls)
                if not skip_steps:
                    full = _fetch_full_protocol(pid, token)
                    time.sleep(RATE_LIMIT_DELAY)
                    protocol = _extract_protocol_fields(full if full else raw)
                else:
                    protocol = _extract_protocol_fields(raw)

                # Save individual protocol file
                dest = output_dir / f"{pid}.json"
                dest.write_text(json.dumps(protocol, indent=2, ensure_ascii=False), encoding="utf-8")

                seen_ids.add(pid)
                fetched_this_kw += 1
                total_new += 1

                # Lightweight index entry (no full steps — kept in individual files)
                index_entries.append({
                    "id": pid,
                    "title": protocol["title"],
                    "uri": protocol["uri"],
                    "doi": protocol["doi"],
                    "description": protocol["description"][:300],
                    "keywords": protocol["keywords"],
                    "authors": protocol["authors"],
                    "published_on": protocol["published_on"],
                    "file": str(dest.relative_to(output_dir.parent)),
                })

                log.info(f"  [{total_new:>4}] {pid}: {protocol['title'][:70]}")

            if not has_more:
                break
            page_id += 1

        log.info(f"  Fetched {fetched_this_kw} new protocols for '{keyword}'")

    # Write / overwrite the index
    index_path.write_text(json.dumps(index_entries, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info(f"\nDone. {total_new} new protocols cached. Total in index: {len(index_entries)}")
    log.info(f"Index: {index_path}")
    log.info(f"Protocols dir: {output_dir}")
    return index_entries


def main():
    parser = argparse.ArgumentParser(description="Fetch protocols from protocols.io into local cache.")
    parser.add_argument(
        "--keywords",
        nargs="+",
        default=DEFAULT_KEYWORDS,
        help="Search keywords to use. Defaults to a broad biology set.",
    )
    parser.add_argument(
        "--max-per-keyword",
        type=int,
        default=50,
        help="Max protocols to fetch per keyword (default: 50).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/protocols"),
        help="Directory to write protocol JSON files (default: data/protocols).",
    )
    parser.add_argument(
        "--skip-steps",
        action="store_true",
        help="Skip the per-protocol full fetch (halves API calls / time). "
             "The list endpoint omits step text anyway, so this matches the "
             "existing cached corpus while covering ~2x more protocols.",
    )
    args = parser.parse_args()

    token = _get_token()

    log.info(f"Output dir: {args.output_dir.resolve()}")
    log.info(f"Keywords ({len(args.keywords)}): {args.keywords[:5]}{'...' if len(args.keywords) > 5 else ''}")
    log.info(f"Max per keyword: {args.max_per_keyword}")
    log.info(f"Estimated max protocols: {len(args.keywords) * args.max_per_keyword} (before deduplication)")

    fetch_and_cache(
        keywords=args.keywords,
        output_dir=args.output_dir,
        max_per_keyword=args.max_per_keyword,
        token=token,
        skip_steps=args.skip_steps,
    )


if __name__ == "__main__":
    main()
