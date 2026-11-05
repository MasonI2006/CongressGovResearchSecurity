"""Configuration and constants for the Congress.gov term-search downloader."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


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
    from datetime import date

    def start_of(n):
        year = congress_years(n)[0]
        return date(year, 1, 3) if n >= 74 else date(year, 3, 4)

    return start_of(congress), start_of(congress + 1)


# Congresses to cover. Configurable via env var CONGRESSES="119", "118,119", or "110-119".
CONGRESSES = parse_congresses(os.environ.get("CONGRESSES", "119"))

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
