"""Shared pieces of the Congress.gov term search tool: settings, search terms
and matching, the Congress.gov API client (with cache and rate limiting), PDF
text extraction, and the per-search progress store."""
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import warnings
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
from pypdf import PdfReader

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)


# ============================================================================
# Settings and search folders
# ============================================================================

ROOT = Path(__file__).resolve().parent


def _load_env(path: Path) -> dict:
    env = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


_env_file = _load_env(ROOT / ".env")
API_KEY = os.environ.get("CONGRESS_API_KEY") or _env_file.get("CONGRESS_API_KEY")

API_BASE = "https://api.congress.gov/v3"


def parse_congresses(spec: str) -> list[int]:
    """Parse a Congress selection such as "119", "117,119", or "110-118,119"
    into a sorted, de-duplicated list of Congress numbers."""
    selected = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            low, high = (int(p) for p in part.split("-", 1))
            if low > high:
                low, high = high, low
            selected.update(range(low, high + 1))
        else:
            selected.add(int(part))
    if not selected or min(selected) < 1:
        raise ValueError(f"Invalid Congress selection: {spec!r}")
    return sorted(selected)


def congress_ordinal(congress: int) -> str:
    """119 -> "119th", 101 -> "101st", 112 -> "112th" (as used in congress.gov URLs)."""
    if 10 <= congress % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(congress % 10, "th")
    return f"{congress}{suffix}"


def congress_years(congress: int) -> tuple[int, int]:
    """Calendar years of a Congress's two sessions (the 119th is 2025-2026)."""
    first = 1787 + 2 * congress
    return first, first + 1


def congress_date_range(congress: int):
    """[start, end) dates of a Congress. Terms begin January 3 from the 74th
    Congress (1935) onward and March 4 before that (20th Amendment)."""
    def start_of(n):
        year = congress_years(n)[0]
        return date(year, 1, 3) if n >= 74 else date(year, 3, 4)

    return start_of(congress), start_of(congress + 1)


# Rate limiting: api.data.gov keys default to 5,000 requests/hour.
# Stay comfortably under that.
MAX_REQUESTS_PER_HOUR = int(os.environ.get("MAX_REQUESTS_PER_HOUR", "4500"))
MIN_REQUEST_INTERVAL = 3600.0 / MAX_REQUESTS_PER_HOUR

PAGE_SIZE = 250  # max page size permitted by the API

CACHE_DIR = Path(os.environ.get("CONGRESS_API_CACHE_DIR", ROOT / "cache")).resolve()
SEARCHES_DIR = Path(os.environ.get("CONGRESS_SEARCHES_DIR", ROOT / "searches")).resolve()
CACHE_DIR.mkdir(parents=True, exist_ok=True)

USER_AGENT = "CongressGovTermSearch/1.0 (research tool)"

# Set by activate_search(): the active search and its folders. Every search has
# its own folder (PDFs, CSVs, SQLite, sidecars, raw API snapshots, progress);
# all searches share CACHE_DIR, so a second search over the same Congresses
# mostly reuses cached API responses.
SEARCH = None
ARCHIVE_DIR = None
STATE_DIR = None
RAW_API_DIR = None


def search_dir(name: str) -> Path:
    return SEARCHES_DIR / name


def activate_search(spec):
    global SEARCH, ARCHIVE_DIR, STATE_DIR, RAW_API_DIR
    SEARCH = spec
    ARCHIVE_DIR = search_dir(spec.name)
    STATE_DIR = ARCHIVE_DIR / "state"
    RAW_API_DIR = ARCHIVE_DIR / "raw_api"
    for d in (ARCHIVE_DIR, STATE_DIR):
        d.mkdir(parents=True, exist_ok=True)
    for term in spec.terms:
        term_pdf_dir(term).mkdir(parents=True, exist_ok=True)


def term_dir(term: str) -> Path:
    """Folder holding one term's results. A one-term search keeps its PDFs at
    the top of the search folder; a multi-term search has terms/<folder>/."""
    if not SEARCH.multi_term:
        return ARCHIVE_DIR
    return ARCHIVE_DIR / "terms" / SEARCH.folders[term]


def term_pdf_dir(term: str) -> Path:
    return term_dir(term) / "PDFs"


def all_pdf_dirs():
    return [term_pdf_dir(term) for term in SEARCH.terms]


# ============================================================================
# Search terms and matching
# ============================================================================

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")
CONTEXT_CHARS = 220


class SearchSpecError(ValueError):
    pass


def slugify(text: str) -> str:
    slug = text.lower().replace("*", "-wild")
    slug = re.sub(r"[^a-z0-9]+", "_", slug).strip("_")
    return slug[:60].strip("_") or "term"


def _is_word_edge(ch: str) -> bool:
    return ch == "*" or ch.isalnum() or ch == "_"


def phrase_pattern(term: str) -> str:
    """Regex for a plain phrase: words joined by flexible whitespace, '*' as an
    in-word wildcard, whole-word boundaries where the phrase edge is a word char."""
    words = term.split()
    body = r"\s+".join(r"\w*".join(re.escape(part) for part in word.split("*")) for word in words)
    start = r"(?<!\w)" if _is_word_edge(term.strip()[0]) else ""
    end = r"(?!\w)" if _is_word_edge(term.strip()[-1]) else ""
    return start + body + end


def normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text)


@dataclass
class SearchSpec:
    name: str
    terms: list            # list of term strings, in the user's order
    regex: bool = False
    case_sensitive: bool = False
    folders: dict = field(default_factory=dict)   # term -> per-term folder name
    created_at: str = ""

    def __post_init__(self):
        if not NAME_RE.match(self.name or ""):
            raise SearchSpecError(
                f"invalid search name {self.name!r}: use letters, digits, '_' or '-' (max 80 characters)")
        cleaned = []
        for term in self.terms:
            term = (term or "").strip()
            if term and term not in cleaned:
                cleaned.append(term)
        if not cleaned:
            raise SearchSpecError("no search terms given")
        self.terms = cleaned
        flags = 0 if self.case_sensitive else re.IGNORECASE
        self._compiled = []
        for term in self.terms:
            if not self.regex and not term.replace("*", "").strip():
                raise SearchSpecError(f"term {term!r} has no letters or digits")
            pattern = term if self.regex else phrase_pattern(term)
            try:
                self._compiled.append((term, re.compile(pattern, flags)))
            except re.error as e:
                raise SearchSpecError(f"term {term!r} is not a valid regular expression: {e}") from None
        # Stable, unique folder names for multi-term searches.
        used = set(self.folders.values())
        for term in self.terms:
            if term in self.folders:
                continue
            base = slugify(term)
            folder, n = base, 2
            while folder in used:
                folder, n = f"{base}_{n}", n + 1
            self.folders[term] = folder
            used.add(folder)
        self.folders = {t: self.folders[t] for t in self.terms}

    @property
    def multi_term(self) -> bool:
        return len(self.terms) > 1

    def same_search(self, other: "SearchSpec") -> bool:
        return (self.terms == other.terms and self.regex == other.regex
                and self.case_sensitive == other.case_sensitive)

    def describe(self) -> str:
        mode = "regular expressions" if self.regex else "phrases (whole words, * wildcard)"
        case = "case-sensitive" if self.case_sensitive else "case-insensitive"
        return f"{len(self.terms)} term(s), {mode}, {case}"

    def find_matches(self, text: str):
        """All occurrences of every term in the text, ordered by position.
        Returns dicts: term, start, end, context, surface (the matched text)."""
        if not text:
            return []
        norm = normalize_whitespace(text)
        found = []
        for term, regex in self._compiled:
            for m in regex.finditer(norm):
                if m.end() == m.start():
                    continue
                start = max(0, m.start() - CONTEXT_CHARS)
                end = min(len(norm), m.end() + CONTEXT_CHARS)
                found.append({"term": term, "start": m.start(), "end": m.end(),
                              "context": norm[start:end].strip(), "surface": m.group(0)})
        found.sort(key=lambda item: (item["start"], self.terms.index(item["term"])))
        return found

    def terms_in(self, text: str) -> set:
        if not text:
            return set()
        norm = normalize_whitespace(text)
        return {term for term, regex in self._compiled if regex.search(norm)}

    # --- persistence -------------------------------------------------------
    def to_json(self) -> dict:
        return {"name": self.name, "terms": self.terms, "regex": self.regex,
                "case_sensitive": self.case_sensitive, "term_folders": self.folders,
                "created_at": self.created_at}

    def save(self, folder: Path):
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "search.json").write_text(json.dumps(self.to_json(), indent=2, ensure_ascii=False) + "\n",
                                            encoding="utf-8")

    @classmethod
    def load(cls, folder: Path) -> "SearchSpec":
        data = json.loads((folder / "search.json").read_text(encoding="utf-8"))
        return cls(name=data["name"], terms=data["terms"], regex=data.get("regex", False),
                   case_sensitive=data.get("case_sensitive", False),
                   folders=dict(data.get("term_folders") or {}), created_at=data.get("created_at", ""))


def read_terms_file(path: Path) -> list:
    """One term per line; blank lines and lines starting with '#' are ignored."""
    try:
        lines = Path(path).read_text(encoding="utf-8-sig").splitlines()
    except OSError as e:
        raise SearchSpecError(f"cannot read terms file {path}: {e}") from None
    return [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]


# ============================================================================
# Congress.gov API client: cache, rate limiting, retries
# ============================================================================

_throttle_lock = threading.Lock()
_last_request_time = [0.0]

_web_lock = threading.Lock()
_last_web_request_time = [0.0]
MIN_WEB_INTERVAL = 0.15  # ~6-7 req/s to www.congress.gov, polite but fast

_session = requests.Session()
_session.headers.update({"User-Agent": USER_AGENT})

# Simple counters for reporting.
STATS = {
    "api_calls": 0,
    "cache_hits": 0,
    "file_downloads": 0,
    "file_cache_hits": 0,
    "retries": 0,
}
LAST_API_RESPONSE = {"http_status": None, "retrieved_at": None, "cached": False}


def sanitize_url(url):
    """Drop any api_key query parameter from a URL (non-strings pass through)."""
    if not isinstance(url, str):
        return url
    parts = urlsplit(url)
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
             if key.lower() != "api_key"]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def redact_text(value: str) -> str:
    text = str(value)
    if API_KEY:
        text = text.replace(API_KEY, "[REDACTED]")
    return re.sub(r"([?&]api_key=)[^&\s]+", r"\1[REDACTED]", text, flags=re.IGNORECASE)


def _utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _throttle():
    with _throttle_lock:
        now = time.monotonic()
        wait = MIN_REQUEST_INTERVAL - (now - _last_request_time[0])
        if wait > 0:
            time.sleep(wait)
        _last_request_time[0] = time.monotonic()


def _throttle_web():
    with _web_lock:
        now = time.monotonic()
        wait = MIN_WEB_INTERVAL - (now - _last_web_request_time[0])
        if wait > 0:
            time.sleep(wait)
        _last_web_request_time[0] = time.monotonic()


def _cache_path(url: str, subdir: str) -> Path:
    h = hashlib.sha256(url.encode("utf-8")).hexdigest()
    d = CACHE_DIR / subdir
    d.mkdir(parents=True, exist_ok=True)
    return d / h


def _api_request_parts(path: str, params: dict | None):
    """Full URL, request params, and cache file for an API request. The cache
    key is the URL plus the params (the api key is never part of it)."""
    url = path if path.startswith("http") else f"{API_BASE}/{path.lstrip('/')}"
    params = dict(params or {})
    params.setdefault("format", "json")
    cache_key = url + "?" + json.dumps(dict(sorted(params.items())), sort_keys=True)
    return url, params, _cache_path(cache_key, "api")


def is_cached(path: str, params: dict | None = None) -> bool:
    """True if api_get(path, params) would be answered from the cache."""
    return _api_request_parts(path, params)[2].exists()


def list_items(data: dict) -> list:
    """The records in an API list response: the first list-valued field that
    isn't 'pagination'/'request'."""
    for key, value in data.items():
        if isinstance(value, list) and key not in ("pagination", "request"):
            return value
    return []


def api_get(path: str, params: dict | None = None, max_attempts: int = 8) -> dict:
    """GET a congress.gov API endpoint (path is relative to API_BASE, or a full URL).

    Results are cached to disk on the full URL (including query params, api key
    excluded from the cache key so caching works across processes safely).
    """
    url, params, cpath = _api_request_parts(path, params)
    if cpath.exists():
        STATS["cache_hits"] += 1
        cached_at = datetime.fromtimestamp(cpath.stat().st_mtime, timezone.utc)
        LAST_API_RESPONSE.update({
            "http_status": 200,
            "retrieved_at": cached_at.isoformat(timespec="seconds").replace("+00:00", "Z"),
            "cached": True,
        })
        return json.loads(cpath.read_text(encoding="utf-8"))

    if not API_KEY:
        raise SystemExit("CONGRESS_API_KEY not found. Put CONGRESS_API_KEY=... in the .env file "
                         "(free key: https://api.congress.gov/sign-up/).")
    req_params = dict(params)
    req_params["api_key"] = API_KEY

    backoff = 1.0
    for _attempt in range(max_attempts):
        _throttle()
        try:
            resp = _session.get(url, params=req_params, timeout=30)
        except requests.RequestException:
            STATS["retries"] += 1
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue

        STATS["api_calls"] += 1
        LAST_API_RESPONSE.update({"http_status": resp.status_code, "retrieved_at": _utc_now(), "cached": False})
        if resp.status_code == 200:
            data = resp.json()
            cpath.write_text(json.dumps(data), encoding="utf-8")
            return data
        if resp.status_code == 429:
            STATS["retries"] += 1
            time.sleep(max(backoff, 5))
            backoff = min(backoff * 2, 120)
            continue
        if resp.status_code >= 500:
            STATS["retries"] += 1
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue
        # 4xx (other than 429): don't retry, but don't crash the whole run.
        raise RuntimeError(f"Congress.gov API returned HTTP {resp.status_code} for {sanitize_url(url)}")
    raise RuntimeError(f"Failed to fetch {url} after retries")


def _fetch_file(url: str, subdir: str, timeout: int):
    """Download a document from congress.gov's web tier (not the API, so not
    counted against the API quota), cached on disk by URL. Returns the response
    bytes, or None if it is missing or keeps failing."""
    cpath = _cache_path(url, subdir)
    if cpath.exists():
        STATS["file_cache_hits"] += 1
        return cpath.read_bytes()
    backoff = 1.0
    for _attempt in range(6):
        _throttle_web()
        try:
            resp = _session.get(url, timeout=timeout)
        except requests.RequestException:
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
            continue
        if resp.status_code == 200:
            STATS["file_downloads"] += 1
            cpath.write_bytes(resp.content)
            return resp.content
        if resp.status_code == 429 or resp.status_code >= 500:
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
            continue
        return None  # 404 or another client error
    return None


def fetch_text(url: str) -> str | None:
    """Download a text/HTML/XML document."""
    data = _fetch_file(url, "text", timeout=30)
    return None if data is None else data.decode("utf-8", errors="replace")


def fetch_binary(url: str) -> bytes | None:
    """Download a binary (PDF) document."""
    return _fetch_file(url, "bin", timeout=60)


def api_paginate(path: str, params: dict | None = None, result_key: str | None = None):
    """Yield all items across all pages for a list endpoint, using max page size."""
    params = dict(params or {})
    params.setdefault("limit", PAGE_SIZE)
    offset = 0
    while True:
        params["offset"] = offset
        data = api_get(path, params)
        items = data.get(result_key, []) if result_key else list_items(data)
        for item in items:
            yield item
        pagination = data.get("pagination", {})
        next_url = pagination.get("next")
        if not next_url or not items:
            break
        offset += params["limit"]


# ============================================================================
# PDF text extraction
# ============================================================================

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _pdftotext_pages(pdf_bytes: bytes):
    executable = shutil.which("pdftotext")
    if not executable:
        return []
    try:
        result = subprocess.run([executable, "-", "-"], input=pdf_bytes,
                                capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    text = result.stdout.decode("utf-8", errors="replace")
    page_texts = text.split("\f")
    if page_texts and not page_texts[-1].strip():
        page_texts.pop()
    return [(page_number, page) for page_number, page in enumerate(page_texts, start=1)]


def extract_pdf_pages_text(pdf_bytes: bytes):
    """Return a list of (page_number_1_indexed, text) tuples. Best-effort; a
    page that fails to extract yields an empty string rather than raising."""
    pages = []
    try:
        reader = PdfReader(BytesIO(pdf_bytes))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception:
                pass
        num_pages = len(reader.pages)
    except Exception:
        return _pdftotext_pages(pdf_bytes)
    for i in range(num_pages):
        text = ""
        try:
            text = reader.pages[i].extract_text() or ""
        except Exception:
            text = ""
        pages.append((i + 1, text))
    pypdf_chars = sum(len(text.strip()) for _, text in pages)
    if pypdf_chars < max(80, num_pages * 1250):
        fallback_pages = _pdftotext_pages(pdf_bytes)
        fallback_chars = sum(len(text.strip()) for _, text in fallback_pages)
        if fallback_chars > pypdf_chars:
            return fallback_pages
    return pages


# ============================================================================
# Text helpers
# ============================================================================

def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    return soup.get_text(separator=" ")


def find_matches(text: str):
    """Occurrences of the active search's terms in text, tolerant of line/page
    breaks between words. Returns dicts: term, start, end, context, surface."""
    return SEARCH.find_matches(text)


def terms_in(text: str) -> set:
    """Which of the active search's terms occur in text."""
    return SEARCH.terms_in(text)


# ============================================================================
# Progress store: processed documents, dedupe, counters, CSV writers
# ============================================================================

_lock = threading.RLock()

METADATA_FIELDS = [
    "filename", "sha256", "collection", "congress", "document_type",
    "document_number", "title", "date", "congress_url", "pdf_url",
    "stable_id", "source_system", "api_base", "api_detail_url", "retrieved_at",
    "http_status", "pdf_sha256", "pdf_bytes", "pdf_page_count", "pdf_has_text_layer",
    "text_source_used", "raw_api_json_path", "update_date", "api_fields_json",
    "related_document_ids", "related_locations_json", "sidecar_path", "matched_terms",
]
MATCHES_FIELDS = [
    "filename", "document_identifier", "collection", "congress", "page_number",
    "matching_text_context", "char_location", "api_endpoint", "stable_id", "pdf_sha256",
    "character_offset", "text_source_used", "matched_surface_form", "match_id", "term",
]

# Paths and persisted state for the active search; set by init().
_metadata_path = None
_matches_path = None
_state_path = None
_processed_doc_ids = set()
_sha_to_filename = {}
_pending_matches = set()
_processing_failures = {}
# Parent records (a bill, hearing, meeting, CR issue, ...) whose child documents
# were all processed, keyed to the list entry's update date at that time. While
# the update date is unchanged, crawlers skip the parent without refetching its
# text/detail endpoint, so re-runs don't repeat every per-item API call.
_parents_done = {}
_processed_since_checkpoint = 0
_STATE_CHECKPOINT_INTERVAL = 25

# in-memory record counters, seeded from persisted state so counts accumulate
# across interrupted/resumed runs instead of resetting to zero each time.
# (collection, congress) -> {"examined": n, "full_text_searched": bool, "matches": n,
#                            "unique_pdfs": set(), "examined_ids": set()}
COUNTERS = {}


def _new_counter(full_text_searched=False):
    return {"examined": 0, "full_text_searched": full_text_searched, "matches": 0,
            "unique_pdfs": set(), "examined_ids": set()}


def _load_counter(c):
    return {
        "examined": c.get("examined", 0),
        "full_text_searched": c.get("full_text_searched", False),
        "matches": c.get("matches", 0),
        "unique_pdfs": set(c.get("unique_pdfs", [])),
        "examined_ids": set(c.get("examined_ids", [])),
    }


def init():
    """Load the active search's progress and make sure its CSVs exist.
    Call after activate_search()."""
    global _metadata_path, _matches_path, _state_path, _processed_doc_ids, _sha_to_filename
    global _pending_matches, _processing_failures, _parents_done, _processed_since_checkpoint
    _metadata_path = ARCHIVE_DIR / "metadata.csv"
    _matches_path = ARCHIVE_DIR / "matches.csv"
    _state_path = STATE_DIR / "dedupe_state.json"
    state = (json.loads(_state_path.read_text(encoding="utf-8")) if _state_path.exists()
             else {"processed_doc_ids": [], "sha_to_filename": {}})
    _processed_doc_ids = set(state["processed_doc_ids"])
    _sha_to_filename = dict(state["sha_to_filename"])
    _pending_matches = set(state.get("pending_matches", []))
    _processing_failures = dict(state.get("processing_failures", {}))
    _parents_done = dict(state.get("parents_done", {}))
    _processed_since_checkpoint = 0
    COUNTERS.clear()
    for congress, by_coll in state.get("collection_counts_by_congress", {}).items():
        for coll, c in by_coll.items():
            COUNTERS[(coll, int(congress))] = _load_counter(c)
    _ensure_csv(_metadata_path, METADATA_FIELDS)
    _ensure_csv(_matches_path, MATCHES_FIELDS)


def match_id(stable_id, pdf_sha256, document_id, page, offset, context, term) -> str:
    """Deterministic ID for one occurrence, so re-runs upsert instead of duplicating."""
    parts = [stable_id, pdf_sha256, document_id, page, offset, context, term]
    seed = "|".join("" if part is None else str(part) for part in parts)
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def counter(collection: str, congress: int, full_text_searched=False):
    with _lock:
        return COUNTERS.setdefault((collection, int(congress)), _new_counter(full_text_searched))


def _save_state():
    by_congress = {}
    for (coll, congress), c in COUNTERS.items():
        by_congress.setdefault(str(congress), {})[coll] = {
            k: (sorted(v) if isinstance(v, set) else v) for k, v in c.items()
        }
    payload = json.dumps({
        "processed_doc_ids": sorted(_processed_doc_ids),
        "sha_to_filename": _sha_to_filename,
        "pending_matches": sorted(_pending_matches),
        "processing_failures": _processing_failures,
        "parents_done": _parents_done,
        "collection_counts_by_congress": by_congress,
    })
    temp = _state_path.with_suffix(".json.tmp")
    temp.write_text(payload, encoding="utf-8")
    temp.replace(_state_path)


def _ensure_csv(path, fields):
    if not path.exists():
        with open(path, "w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=fields).writeheader()
        return
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        old_fields = reader.fieldnames or []
        if old_fields == fields:
            return
        rows = list(reader)
    _write_csv(path, rows, list(dict.fromkeys(old_fields + fields)))


def clean_text(value):
    if value is None:
        return ""
    return "".join(ch for ch in str(value) if ch in "\n\t" or ord(ch) >= 32)


def _write_csv(path, rows, fields):
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: clean_text(value) for key, value in row.items()})
    temp.replace(path)


def already_processed(doc_id: str) -> bool:
    return doc_id in _processed_doc_ids


def mark_processed(doc_id: str):
    global _processed_since_checkpoint
    with _lock:
        _processed_doc_ids.add(doc_id)
        _pending_matches.discard(doc_id)
        _processing_failures.pop(doc_id, None)
        _processed_since_checkpoint += 1
        if _processed_since_checkpoint >= _STATE_CHECKPOINT_INTERVAL:
            _save_state()
            _processed_since_checkpoint = 0


def count_parents_done(prefix: str) -> int:
    """How many finished parent records have keys starting with prefix."""
    return sum(1 for key in _parents_done if key.startswith(prefix))


def parent_done(key: str, update_marker) -> bool:
    """True if every document under this parent was processed while the
    parent's list-entry update date was the same as it is now."""
    return key in _parents_done and _parents_done[key] == (update_marker or "")


def mark_parent_done(key: str, update_marker, doc_ids):
    """Record the parent as done only if all its child documents are processed
    (a matched document that failed persistence keeps the parent open)."""
    with _lock:
        if all(doc_id in _processed_doc_ids for doc_id in doc_ids):
            _parents_done[key] = update_marker or ""


def mark_match_pending(doc_id: str, collection: str):
    with _lock:
        _pending_matches.add(doc_id)
        _processing_failures.pop(doc_id, None)
        _processing_failures.setdefault(doc_id, {"collection": collection, "error": None})
        _save_state()


def record_processing_failure(doc_id: str, collection: str, error: str):
    with _lock:
        if doc_id not in _pending_matches:
            return
        _processing_failures[doc_id] = {
            "collection": collection,
            "error": clean_text(error),
            "failed_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        }
        _save_state()


def pending_match_failures():
    with _lock:
        return [{"document_identifier": doc_id,
                 **_processing_failures.get(doc_id, {"collection": "unknown", "error": None})}
                for doc_id in sorted(_pending_matches) if doc_id not in _processed_doc_ids]


def note_examined(collection: str, congress: int, full_text_searched: bool, doc_id: str | None = None):
    with _lock:
        c = counter(collection, congress)
        examined_ids = c.setdefault("examined_ids", set())
        if doc_id is None or doc_id not in examined_ids:
            c["examined"] += 1
            if doc_id is not None:
                examined_ids.add(doc_id)
        c["full_text_searched"] = c["full_text_searched"] or full_text_searched


def safe_filename(doc_id: str, url: str) -> str:
    base = re.sub(r"[^A-Za-z0-9_.-]", "_", doc_id)
    ext = ".pdf"
    return f"{base}{ext}"


def filename_for_hash(sha256: str, candidate: str) -> str:
    return _sha_to_filename.get(sha256, candidate)


def remember_pdf(sha256: str, filename: str, collection: str, congress: int, match_count: int):
    with _lock:
        _sha_to_filename[sha256] = filename
        c = counter(collection, congress, full_text_searched=True)
        c["unique_pdfs"].add(sha256)
        c["matches"] += match_count


def upsert_metadata(row):
    with _lock:
        with _metadata_path.open(newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            fields = list(dict.fromkeys((reader.fieldnames or []) + METADATA_FIELDS))
            rows = list(reader)
        key = row.get("pdf_sha256") or row.get("sha256")
        index = next((i for i, current in enumerate(rows)
                      if (current.get("pdf_sha256") or current.get("sha256")) == key), None)
        if index is None:
            rows.append({field: "" for field in fields} | row)
        else:
            current = rows[index]
            locations = _json_list(current.get("related_locations_json"))
            document_ids = _json_list(current.get("related_document_ids"))
            document_ids.extend(_json_list(row.get("related_document_ids")))
            location = {field: row.get(field) for field in ("stable_id", "collection", "congress", "document_type",
                                                              "document_number", "api_detail_url", "pdf_url", "sidecar_path")}
            if location not in locations:
                locations.append(location)
            terms = _json_list(current.get("matched_terms"))
            terms += [t for t in _json_list(row.get("matched_terms")) if t not in terms]
            rows[index] = {**current, **row,
                           "matched_terms": json.dumps(terms, ensure_ascii=False),
                           "related_document_ids": json.dumps(sorted(set(document_ids)), ensure_ascii=False),
                           "related_locations_json": json.dumps(locations, ensure_ascii=False)}
        _write_csv(_metadata_path, rows, fields)


def upsert_matches(rows_to_add):
    with _lock:
        with _matches_path.open(newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            fields = list(dict.fromkeys((reader.fieldnames or []) + MATCHES_FIELDS))
            rows = list(reader)
        seen = {row.get("match_id") for row in rows if row.get("match_id")}
        for row in rows_to_add:
            match_id = row.get("match_id")
            if match_id and match_id in seen:
                continue
            rows.append({field: "" for field in fields} | row)
            if match_id:
                seen.add(match_id)
        _write_csv(_matches_path, rows, fields)


def _json_list(value):
    if not value:
        return []
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else []
    except (TypeError, json.JSONDecodeError):
        return []


def flush():
    global _processed_since_checkpoint
    _save_state()
    _processed_since_checkpoint = 0
