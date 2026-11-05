"""Generate archive reports from persisted CSV and SQLite contents."""
import csv
import json
import sqlite3
from collections import Counter

from . import config


def _terms_of(row):
    try:
        terms = json.loads(row.get("matched_terms") or "[]")
    except json.JSONDecodeError:
        terms = []
    return [t for t in terms if t in config.SEARCH.terms] or [config.SEARCH.terms[0]]


def write_term_views():
    """For a multi-term search, write each term folder's own metadata.csv and
    matches.csv (the rows of the search-wide CSVs that belong to that term).
    The search-wide CSVs and SQLite remain the complete record."""
    spec = config.SEARCH
    if not spec.multi_term:
        return
    archive = config.ARCHIVE_DIR
    with (archive / "metadata.csv").open(newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        meta_fields, metadata = reader.fieldnames or [], list(reader)
    with (archive / "matches.csv").open(newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        match_fields, matches = reader.fieldnames or [], list(reader)
    for term in spec.terms:
        folder = config.term_dir(term)
        folder.mkdir(parents=True, exist_ok=True)
        for name, fields, rows in (
            ("metadata.csv", meta_fields, [r for r in metadata if term in _terms_of(r)]),
            ("matches.csv", match_fields, [r for r in matches if (r.get("term") or spec.terms[0]) == term]),
        ):
            with (folder / name).open("w", newline="", encoding="utf-8") as stream:
                writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(rows)
        (folder / "TERM.txt").write_text(
            f"Term: {term}\nSearch: {spec.name} ({spec.describe()})\n"
            "These CSVs are the rows of the search-wide metadata.csv/matches.csv for this term.\n",
            encoding="utf-8")


def persist_collection_counters(counters):
    """counters maps (collection, congress) -> stats. Writes per-Congress rows to
    collection_congress_stats and all-Congress totals to collection_stats."""
    db_path = config.ARCHIVE_DIR / "archive.sqlite"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    totals = {}
    with sqlite3.connect(db_path) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS collection_stats (
            collection TEXT PRIMARY KEY, records_examined INTEGER NOT NULL,
            full_text_searched INTEGER NOT NULL, matches INTEGER NOT NULL,
            unique_pdfs INTEGER NOT NULL
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS collection_congress_stats (
            collection TEXT NOT NULL, congress INTEGER NOT NULL, records_examined INTEGER NOT NULL,
            full_text_searched INTEGER NOT NULL, matches INTEGER NOT NULL,
            unique_pdfs INTEGER NOT NULL, PRIMARY KEY(collection, congress)
        )""")
        for (collection, congress), stats in counters.items():
            db.execute("""INSERT INTO collection_congress_stats VALUES(?,?,?,?,?,?)
                ON CONFLICT(collection, congress) DO UPDATE SET records_examined=excluded.records_examined,
                full_text_searched=excluded.full_text_searched, matches=excluded.matches,
                unique_pdfs=excluded.unique_pdfs""",
                (collection, int(congress), int(stats.get("examined", 0)),
                 int(bool(stats.get("full_text_searched"))), int(stats.get("matches", 0)),
                 len(stats.get("unique_pdfs", set()))))
            total = totals.setdefault(collection, {"examined": 0, "full_text_searched": False,
                                                   "matches": 0, "unique_pdfs": set()})
            total["examined"] += stats.get("examined", 0)
            total["full_text_searched"] = total["full_text_searched"] or bool(stats.get("full_text_searched"))
            total["matches"] += stats.get("matches", 0)
            total["unique_pdfs"] |= set(stats.get("unique_pdfs", set()))
        for collection, stats in totals.items():
            db.execute("""INSERT INTO collection_stats VALUES(?,?,?,?,?)
                ON CONFLICT(collection) DO UPDATE SET records_examined=excluded.records_examined,
                full_text_searched=excluded.full_text_searched, matches=excluded.matches,
                unique_pdfs=excluded.unique_pdfs""",
                (collection, stats["examined"], int(stats["full_text_searched"]),
                 stats["matches"], len(stats["unique_pdfs"])))


def write_readme(congresses, collections, start_time, end_time, website_ran,
                 website_counts, pdf_verification_ran, pdf_verification, validation, failures=None):
    archive = config.ARCHIVE_DIR
    metadata_path = archive / "metadata.csv"
    matches_path = archive / "matches.csv"
    metadata = []
    matches = []
    if metadata_path.exists():
        with metadata_path.open(newline="", encoding="utf-8-sig") as stream:
            metadata = list(csv.DictReader(stream))
    if matches_path.exists():
        with matches_path.open(newline="", encoding="utf-8-sig") as stream:
            matches = list(csv.DictReader(stream))
    congress_by_file = {row.get("filename"): row.get("congress", "") for row in metadata}
    metadata_by_key = Counter((row.get("congress", ""), row.get("collection", "")) for row in metadata)
    match_by_key = Counter((row.get("congress") or congress_by_file.get(row.get("filename"), ""),
                            row.get("collection", "")) for row in matches)

    spec = config.SEARCH
    lines = [
        f"Congress.gov Term Search: {spec.name}",
        "=" * 52,
        f"Run started: {start_time.isoformat()}",
        f"Run finished: {end_time.isoformat()}",
        f"Congress scope (this run): {', '.join(map(str, congresses))}",
        f"Matching: {spec.describe()}",
        "Terms:",
        *[f"  {term}" + (f"  -> terms/{spec.folders[term]}/" if spec.multi_term else "") for term in spec.terms],
        "",
        "Persisted archive facts:",
        f"  Metadata rows (unique PDF rows): {len(metadata)}",
        f"  Match occurrence rows: {len(matches)}",
        f"  SQLite database: {archive / 'archive.sqlite'}",
    ]
    db_path = archive / "archive.sqlite"
    if db_path.exists():
        with sqlite3.connect(db_path) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "documents" in tables:
                lines.append(f"  SQLite documents: {db.execute('SELECT COUNT(*) FROM documents').fetchone()[0]}")
            if "files" in tables:
                lines.append(f"  SQLite file links: {db.execute('SELECT COUNT(*) FROM files').fetchone()[0]}")
            if "matches" in tables:
                lines.append(f"  SQLite matches: {db.execute('SELECT COUNT(*) FROM matches').fetchone()[0]}")
            if "collection_congress_stats" in tables:
                lines.append("")
                lines.append("Crawl counters by Congress (every Congress crawled into this archive):")
                lines.append("Congress | Collection | Records examined | Full text searched | Occurrences | Unique PDFs")
                for row in db.execute("SELECT congress, collection, records_examined, full_text_searched, matches, "
                                      "unique_pdfs FROM collection_congress_stats ORDER BY congress, collection"):
                    lines.append(" | ".join(map(str, (row[0], row[1], row[2], bool(row[3]), row[4], row[5]))))
            if "collection_stats" in tables:
                lines.append("")
                lines.append("Crawl counters, totals across Congresses:")
                lines.append("Collection | Records examined | Full text searched | Occurrences | Unique PDFs")
                for row in db.execute("SELECT collection, records_examined, full_text_searched, matches, unique_pdfs FROM collection_stats ORDER BY collection"):
                    lines.append(" | ".join(map(str, (row[0], row[1], bool(row[2]), row[3], row[4]))))
    lines.extend([
        "",
        "CSV totals (CRS rows have a blank Congress; the CRS API has no Congress field):",
        "Congress | Collection | Metadata rows | Match occurrences",
    ])
    for key in sorted(set(metadata_by_key) | set(match_by_key), key=lambda k: (str(k[0]).zfill(4), k[1])):
        lines.append(f"{key[0]} | {key[1]} | {metadata_by_key[key]} | {match_by_key[key]}")
    lines.extend(["", "By term:", "Term | PDFs | Match occurrences"])
    occurrences_by_term = Counter(row.get("term") or spec.terms[0] for row in matches)
    for term in spec.terms:
        pdfs = len(list(config.term_pdf_dir(term).glob("*.pdf")))
        lines.append(f"{term} | {pdfs} | {occurrences_by_term[term]}")
    lines.extend([
        "",
        "Verification status:",
        f"  Congress.gov website search: {'ran' if website_ran else 'skipped'}",
    ])
    for (term, congress), count in sorted((website_counts or {}).items(), key=lambda kv: (kv[0][1], kv[0][0])):
        lines.append(f"  Website result count ({term!r}, congress {congress}): "
                     f"{count if count is not None else 'unknown'}")
    lines.append(f"  Local PDF text verification: {'ran' if pdf_verification_ran else 'skipped'}")
    if pdf_verification_ran:
        lines.append(f"  PDFs checked: {pdf_verification.get('checked', 0)}; confirmed: {pdf_verification.get('confirmed', 0)}; discrepancies: {len(pdf_verification.get('discrepancies', []))}")
    lines.extend([
        f"  Archive validation: {'PASS' if validation.get('passed') else 'FAIL'}",
        f"  Validation report: {archive / 'validation_report.txt'}",
        "",
        f"Matched documents not marked processed: {len(failures or [])}",
    ])
    for failure in failures or []:
        lines.append(f"  {failure.get('collection')} | {failure.get('document_identifier')} | {failure.get('error') or 'interrupted before completion'}")
    lines.extend([
        "",
        "Scope and known limitations:",
        "  CRS products have no Congress field; the crawler uses the configured publish-date range as its scope basis.",
        "  CRS products use the Congress.gov /crsreport/{id} endpoint and currentVersion metadata.",
        "  The Bound Congressional Record API exposes only page-range metadata (no text or PDF links), so it is probed but not searched.",
        "  Missing optional API values remain null; empty API lists remain empty lists.",
        "  Search coverage is limited to the configured Congresses and collections exposed by the implemented API crawlers.",
        "  A document counts as examined only where the API exposes its text; older Congresses expose less.",
    ])
    (archive / "README.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
