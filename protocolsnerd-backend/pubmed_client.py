"""
Native PubMed client for blended protocol/literature search.

No MCP, no Node — reuses the same NCBI E-utilities infrastructure already used
for taxonomy lookups in concept_expansion.py, and adds article search plus a
full-text fallback chain (PMC -> Europe PMC -> Unpaywall).

Pipeline:
  search_pubmed()   esearch(db=pubmed) -> PMIDs, then efetch -> title + abstract
                    + metadata. Cheap; used for ranking and display.
  fetch_fulltext()  PMC BioC -> Europe PMC fullTextXML -> Unpaywall PDF/landing.
                    Expensive; called ONLY for results that surface in the blend.
  extract_methods() pull the Materials and Methods / Methods / Experimental
                    Procedures section out of fetched full text.

Every result is shaped like a protocols.io result (title/url/doi/description/
authors/keywords) plus source="pubmed" so the frontend renders both identically.
Everything is best-effort: any failure returns empty so the caller can degrade
to protocols.io-only.
"""

from __future__ import annotations

import logging
import os
import re
import ssl
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / "variables.env", override=False)

log = logging.getLogger(__name__)

# Use certifi's CA bundle so HTTPS verification is consistent across hosts and
# proxies (the system trust store can miss roots NCBI/EBI chain to). Falls back
# to the default context if certifi isn't installed.
try:
    import certifi
    _SSL_CTX: Optional[ssl.SSLContext] = ssl.create_default_context(cafile=certifi.where())
except Exception:
    _SSL_CTX = None

_EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
_EUROPEPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest"
_UNPAYWALL = "https://api.unpaywall.org/v2"
_HTTP_UA = "Mozilla/5.0 (compatible; ProtocolsNerdBot/1.0)"

# Section headings that mark a methods section in full text.
_METHODS_HEADINGS = (
    "materials and methods",
    "methods",
    "experimental procedures",
    "materials & methods",
    "methodology",
    "experimental section",
)


def _api_key() -> str:
    return os.getenv("NCBI_API_KEY", "").strip('"').strip()


def _unpaywall_email() -> str:
    return os.getenv("UNPAYWALL_EMAIL", "").strip('"').strip()


def _with_key(params: Dict[str, str]) -> str:
    key = _api_key()
    if key:
        params = {**params, "api_key": key}
    return urllib.parse.urlencode(params)


def _http_text(url: str, timeout: int = 8) -> Optional[str]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _HTTP_UA})
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as r:
            return r.read().decode("utf-8", errors="ignore")
    except Exception as e:
        log.debug(f"HTTP text failed for {url}: {e}")
        return None


def _http_json(url: str, timeout: int = 8) -> Optional[Any]:
    raw = _http_text(url, timeout=timeout)
    if raw is None:
        return None
    try:
        import json
        return json.loads(raw)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Search: esearch -> efetch (title + abstract + metadata)
# ---------------------------------------------------------------------------

# Imperative/scaffolding tokens that mean nothing to PubMed's boolean AND search
# and only shrink the result set. protocols.io's TF-IDF treats these as
# stopwords; PubMed does not, so we strip them before querying.
_PUBMED_STOP = {
    "find", "search", "searching", "get", "locate", "show", "list", "want",
    "wanted", "need", "needed", "looking", "please", "protocol", "protocols",
    "method", "methods", "methodology", "technique", "techniques", "procedure",
    "procedures", "paper", "papers", "publication", "publications", "study",
    "studies", "article", "articles", "for", "using", "that", "can", "allow",
    "allows", "allowing", "i", "we", "me", "my", "to",
}


def _sanitize_for_pubmed(query: str) -> str:
    """Strip natural-language scaffolding so biology keywords reach PubMed."""
    tokens = re.split(r"\s+", query.strip())
    kept = [t for t in tokens if t.lower().strip(".,") not in _PUBMED_STOP]
    cleaned = " ".join(kept).strip()
    # If sanitizing removed almost everything, fall back to the raw query.
    return cleaned if len(cleaned) >= 3 else query.strip()


def search_pubmed(query: str, retmax: int = 5) -> List[Dict[str, Any]]:
    """
    Search PubMed and return up to `retmax` normalized article dicts with
    title, abstract (as `description`), authors, doi, url, pmid, source.
    Returns [] on any failure (caller degrades to protocols.io-only).
    """
    import time
    t_start = time.time()
    query = _sanitize_for_pubmed(query or "")
    if not query:
        return []

    esearch = (
        f"{_EUTILS}/esearch.fcgi?"
        + _with_key({"db": "pubmed", "term": query, "retmax": str(retmax), "retmode": "json", "sort": "relevance"})
    )
    data = _http_json(esearch)
    pmids = (((data or {}).get("esearchresult") or {}).get("idlist")) or []
    if not pmids:
        t_end = time.time()
        log.info(f"PubMed search (no results): {t_end - t_start:.2f}s")
        return []

    efetch = (
        f"{_EUTILS}/efetch.fcgi?"
        + _with_key({"db": "pubmed", "id": ",".join(pmids), "retmode": "xml"})
    )
    xml = _http_text(efetch)
    if not xml:
        t_end = time.time()
        log.info(f"PubMed search (fetch failed): {t_end - t_start:.2f}s")
        return []

    results = _parse_pubmed_xml(xml)
    t_end = time.time()
    log.info(f"PubMed search: {len(results)} results in {t_end - t_start:.2f}s")
    return results


def _parse_pubmed_xml(xml: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        log.debug(f"PubMed XML parse failed: {e}")
        return out

    for art in root.findall(".//PubmedArticle"):
        pmid = _text(art.find(".//PMID"))
        title = _text(art.find(".//ArticleTitle"))
        if not pmid or not title:
            continue
        abstract = " ".join(
            _text(a) for a in art.findall(".//Abstract/AbstractText") if _text(a)
        ).strip()
        authors: List[str] = []
        for a in art.findall(".//AuthorList/Author"):
            last = _text(a.find("LastName"))
            initials = _text(a.find("Initials"))
            name = (f"{last} {initials}".strip() if last else "").strip()
            if name:
                authors.append(name)
        keywords = [
            _text(k) for k in art.findall(".//KeywordList/Keyword") if _text(k)
        ]
        doi = ""
        for aid in art.findall(".//ArticleId"):
            if aid.get("IdType") == "doi":
                doi = (aid.text or "").strip()
                break
        url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
        out.append({
            "id": f"pubmed:{pmid}",
            "pmid": pmid,
            "title": title,
            "uri": "",
            "url": url,
            "doi": doi,
            "description": abstract[:400],
            "abstract": abstract,
            "authors": authors[:6],
            "keywords": keywords[:8],
            "source": "pubmed",
            "score": 0.0,
        })
    return out


def _text(node: Optional[ET.Element]) -> str:
    if node is None:
        return ""
    # itertext() flattens nested markup (e.g. <i>, <sup>) inside titles/abstracts.
    return "".join(node.itertext()).strip()


# ---------------------------------------------------------------------------
# Full text: PMC -> Europe PMC -> Unpaywall (only for surfaced results)
# ---------------------------------------------------------------------------

def fetch_fulltext(pmid: str, doi: str = "") -> str:
    """Best-effort full text via PMC -> Europe PMC -> Unpaywall. "" if none."""
    for fetch in (_fulltext_europepmc, _fulltext_pmc, lambda *_: _fulltext_unpaywall(doi)):
        try:
            text = fetch(pmid, doi)
            if text and len(text) > 500:
                return text
        except Exception as e:
            log.debug(f"fulltext source failed for pmid={pmid}: {e}")
    return ""


def _fulltext_europepmc(pmid: str, doi: str = "") -> str:
    """Europe PMC fullTextXML (open-access subset)."""
    url = f"{_EUROPEPMC}/MED/{pmid}/fullTextXML"
    xml = _http_text(url)
    if not xml:
        return ""
    try:
        root = ET.fromstring(xml)
        return " ".join(t.strip() for t in root.itertext() if t.strip())
    except ET.ParseError:
        return ""


def _fulltext_pmc(pmid: str, doi: str = "") -> str:
    """Resolve PMID -> PMCID, then fetch the BioC/PMC text."""
    link = (
        f"{_EUTILS}/elink.fcgi?"
        + _with_key({"dbfrom": "pubmed", "db": "pmc", "id": pmid, "retmode": "json"})
    )
    data = _http_json(link)
    pmcid = ""
    try:
        linksets = (data or {}).get("linksets") or []
        for ls in linksets:
            for db in ls.get("linksetdbs") or []:
                if db.get("dbto") == "pmc" and db.get("links"):
                    pmcid = str(db["links"][0])
                    break
    except Exception:
        pmcid = ""
    if not pmcid:
        return ""
    efetch = (
        f"{_EUTILS}/efetch.fcgi?"
        + _with_key({"db": "pmc", "id": pmcid, "retmode": "xml"})
    )
    xml = _http_text(efetch)
    if not xml:
        return ""
    try:
        root = ET.fromstring(xml)
        return " ".join(t.strip() for t in root.itertext() if t.strip())
    except ET.ParseError:
        return ""


def _fulltext_unpaywall(doi: str) -> str:
    """Last resort: Unpaywall open-access location (landing/PDF URL note only)."""
    doi = (doi or "").strip()
    email = _unpaywall_email()
    if not doi or not email:
        return ""
    data = _http_json(f"{_UNPAYWALL}/{urllib.parse.quote(doi)}?email={urllib.parse.quote(email)}")
    loc = ((data or {}).get("best_oa_location")) or {}
    # Unpaywall returns a URL, not the article body. We surface the OA URL so the
    # methods extractor has something to point at; we do not scrape arbitrary PDFs.
    return loc.get("url_for_pdf") or loc.get("url") or ""


def extract_methods(fulltext: str) -> str:
    """
    Pull the Materials and Methods section from full text. Returns the section
    body (capped) or "" if no recognizable methods heading is found.
    """
    if not fulltext or len(fulltext) < 200:
        return ""
    low = fulltext.lower()
    start = -1
    for heading in _METHODS_HEADINGS:
        idx = low.find(heading)
        if idx != -1:
            start = idx
            break
    if start == -1:
        return ""
    # End at the next major section heading after the methods block.
    rest = fulltext[start:]
    end_markers = ("\nresults", "\ndiscussion", "\nconclusion", "\nreferences", "\nacknowledg")
    low_rest = rest.lower()
    end = len(rest)
    for m in end_markers:
        i = low_rest.find(m, 200)
        if i != -1:
            end = min(end, i)
    return re.sub(r"\s+", " ", rest[:end]).strip()[:3000]
