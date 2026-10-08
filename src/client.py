"""HTTP client for the Congress.gov API and congress.gov/govinfo file downloads.

Features:
- Disk cache for API JSON responses and text/PDF payloads (keyed by URL).
- Rate limiting to stay under the api.data.gov quota (default 5,000/hr).
- Retry with exponential backoff on 429/5xx/connection errors.
"""
import hashlib
import json
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

from . import config

_lock = threading.Lock()
_last_request_time = [0.0]

_web_lock = threading.Lock()
_last_web_request_time = [0.0]
MIN_WEB_INTERVAL = 0.15  # ~6-7 req/s to www.congress.gov, polite but fast

_session = requests.Session()
_session.headers.update({"User-Agent": config.USER_AGENT})

# Simple counters for reporting.
STATS = {
    "api_calls": 0,
    "cache_hits": 0,
    "file_downloads": 0,
    "file_cache_hits": 0,
    "retries": 0,
}
LAST_API_RESPONSE = {"http_status": None, "retrieved_at": None, "cached": False}


def sanitize_url(url: str) -> str:
    parts = urlsplit(url)
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
             if key.lower() != "api_key"]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def redact_text(value: str) -> str:
    text = str(value)
    if config.API_KEY:
        text = text.replace(config.API_KEY, "[REDACTED]")
    return re.sub(r"([?&]api_key=)[^&\s]+", r"\1[REDACTED]", text, flags=re.IGNORECASE)


def _utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _throttle():
    with _lock:
        now = time.monotonic()
        wait = config.MIN_REQUEST_INTERVAL - (now - _last_request_time[0])
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
    d = config.CACHE_DIR / subdir
    d.mkdir(parents=True, exist_ok=True)
    return d / h


def api_get(path: str, params: dict | None = None, max_attempts: int = 8) -> dict:
    """GET a congress.gov API endpoint (path is relative to API_BASE, or a full URL).

    Results are cached to disk on the full URL (including query params, api key
    excluded from the cache key so caching works across processes safely).
    """
    if path.startswith("http"):
        url = path
    else:
        url = f"{config.API_BASE}/{path.lstrip('/')}"

    params = dict(params or {})
    params.setdefault("format", "json")

    cache_key_params = {k: v for k, v in sorted(params.items())}
    cache_key = url + "?" + json.dumps(cache_key_params, sort_keys=True)
    cpath = _cache_path(cache_key, "api")
    if cpath.exists():
        STATS["cache_hits"] += 1
        cached_at = datetime.fromtimestamp(cpath.stat().st_mtime, timezone.utc)
        LAST_API_RESPONSE.update({
            "http_status": 200,
            "retrieved_at": cached_at.isoformat(timespec="seconds").replace("+00:00", "Z"),
            "cached": True,
        })
        return json.loads(cpath.read_text(encoding="utf-8"))

    req_params = dict(params)
    req_params["api_key"] = config.API_KEY

    backoff = 1.0
    for attempt in range(max_attempts):
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


def fetch_text(url: str) -> str | None:
    """Download a text/HTML/XML document (not counted against API quota; served
    from congress.gov's web tier, not api.congress.gov)."""
    cpath = _cache_path(url, "text")
    if cpath.exists():
        STATS["file_cache_hits"] += 1
        return cpath.read_text(encoding="utf-8", errors="replace")

    backoff = 1.0
    for attempt in range(6):
        _throttle_web()
        try:
            resp = _session.get(url, timeout=30)
        except requests.RequestException:
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
            continue
        if resp.status_code == 200:
            STATS["file_downloads"] += 1
            text = resp.text
            cpath.write_text(text, encoding="utf-8")
            return text
        if resp.status_code == 404:
            return None
        if resp.status_code == 429 or resp.status_code >= 500:
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
            continue
        return None
    return None


def fetch_binary(url: str) -> bytes | None:
    """Download a binary (PDF) document, cached to disk by URL hash."""
    cpath = _cache_path(url, "bin")
    if cpath.exists():
        STATS["file_cache_hits"] += 1
        return cpath.read_bytes()

    backoff = 1.0
    for attempt in range(6):
        _throttle_web()
        try:
            resp = _session.get(url, timeout=60)
        except requests.RequestException:
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
            continue
        if resp.status_code == 200:
            STATS["file_downloads"] += 1
            cpath.write_bytes(resp.content)
            return resp.content
        if resp.status_code == 404:
            return None
        if resp.status_code == 429 or resp.status_code >= 500:
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
            continue
        return None
    return None


def api_paginate(path: str, params: dict | None = None, result_key: str | None = None):
    """Yield all items across all pages for a list endpoint, using max page size."""
    params = dict(params or {})
    params.setdefault("limit", config.PAGE_SIZE)
    offset = 0
    while True:
        params["offset"] = offset
        data = api_get(path, params)
        key = result_key
        if key is None:
            # Guess the result key: the first list-valued key that isn't 'pagination'/'request'
            for k, v in data.items():
                if isinstance(v, list) and k not in ("pagination", "request"):
                    key = k
                    break
        items = data.get(key, []) if key else []
        for item in items:
            yield item
        pagination = data.get("pagination", {})
        next_url = pagination.get("next")
        if not next_url or not items:
            break
        offset += params["limit"]
