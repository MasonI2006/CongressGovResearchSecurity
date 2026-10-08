"""Central store: dedupe logic + incremental CSV writers for metadata/matches."""
import csv
import json
import re
import threading
from datetime import datetime, timezone

from . import config

_lock = threading.RLock()

METADATA_FIELDS = [
    "filename", "sha256", "collection", "congress", "document_type",
    "document_number", "title", "date", "congress_url", "pdf_url",
    "stable_id", "source_system", "api_base", "api_detail_url", "retrieved_at",
    "http_status", "pdf_sha256", "pdf_bytes", "pdf_page_count", "pdf_has_text_layer",
    "text_source_used", "raw_api_json_path", "update_date", "api_fields_json",
    "related_document_ids", "related_locations_json", "sidecar_path",
]
MATCHES_FIELDS = [
    "filename", "document_identifier", "collection", "congress", "page_number",
    "matching_text_context", "char_location", "api_endpoint", "stable_id", "pdf_sha256",
    "character_offset", "text_source_used", "matched_surface_form", "match_id",
]

_metadata_path = config.ARCHIVE_DIR / "metadata.csv"
_matches_path = config.ARCHIVE_DIR / "matches.csv"

# Dedupe state persisted to disk so re-runs don't reprocess/redownload.
_state_path = config.STATE_DIR / "dedupe_state.json"


def _load_state():
    if _state_path.exists():
        return json.loads(_state_path.read_text(encoding="utf-8"))
    return {"processed_doc_ids": [], "sha_to_filename": {}, "collection_counts": {}}


_state = _load_state()
_processed_doc_ids = set(_state["processed_doc_ids"])
_sha_to_filename = dict(_state["sha_to_filename"])
_pending_matches = set(_state.get("pending_matches", []))
_processing_failures = dict(_state.get("processing_failures", {}))
_processed_since_checkpoint = 0
_STATE_CHECKPOINT_INTERVAL = 25

# State written before multi-Congress support kept a single "collection_counts"
# map; every such run covered only the 119th Congress, so it migrates there.
_LEGACY_COUNTER_CONGRESS = 119

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


if "collection_counts_by_congress" in _state:
    for _congress, _by_coll in _state["collection_counts_by_congress"].items():
        for _coll, _c in _by_coll.items():
            COUNTERS[(_coll, int(_congress))] = _load_counter(_c)
else:
    for _coll, _c in _state.get("collection_counts", {}).items():
        COUNTERS[(_coll, _LEGACY_COUNTER_CONGRESS)] = _load_counter(_c)


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


_ensure_csv(_metadata_path, METADATA_FIELDS)
_ensure_csv(_matches_path, MATCHES_FIELDS)


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
            rows[index] = {**current, **row,
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
