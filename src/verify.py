"""Local verification pass: re-extract text from every downloaded PDF and
re-search it for each term the PDF was saved under, cross-checking against
matches.csv. Flags PDFs where extraction disagrees with the API-driven search
that originally flagged them, and back-fills page numbers for matches that were
originally found via an HTML/XML text source (which has no page concept).
"""
import csv
import json
import shutil
import subprocess

from . import config, metadata_capture, pdf_utils, store, text_utils

VERIFY_ENDPOINT = "local_pdf_verification"
VERIFY_CONTEXT = "(confirmed via local PDF re-extraction pass)"


def _pdftotext(path):
    executable = shutil.which("pdftotext")
    if not executable:
        return None
    try:
        result = subprocess.run([executable, str(path), "-"], capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def matched_terms(meta_row):
    """Terms a metadata row's PDF was saved under (all rows written by this
    version record them; fall back to the search's first term)."""
    try:
        terms = json.loads(meta_row.get("matched_terms") or "[]")
    except json.JSONDecodeError:
        terms = []
    return [t for t in terms if t in config.SEARCH.terms] or [config.SEARCH.terms[0]]


def _complete_row(row, meta):
    """Fill the columns a crawl-time match row has (stable_id, pdf_sha256,
    match_id, ...) so verification rows match matches.csv and SQLite."""
    stable_id = row.get("stable_id") or meta.get("stable_id") or row["document_identifier"]
    sha = row.get("pdf_sha256") or meta.get("pdf_sha256") or meta.get("sha256")
    page = row.get("page_number") or ""
    context = row.get("matching_text_context") or VERIFY_CONTEXT
    term = row.get("term") or matched_terms(meta)[0]
    return {**row, "stable_id": stable_id, "pdf_sha256": sha, "page_number": page,
            "matching_text_context": context, "char_location": "", "character_offset": "",
            "text_source_used": "pdf_extraction", "matched_surface_form": "", "term": term,
            "match_id": row.get("match_id") or store.match_id(stable_id, sha, row["document_identifier"],
                                                              page, None, context, term)}


def repair_verification_rows():
    """Complete verification rows written before they carried full columns,
    and make sure every verification row is in SQLite. Returns rows repaired."""
    metadata_path = config.ARCHIVE_DIR / "metadata.csv"
    matches_path = config.ARCHIVE_DIR / "matches.csv"
    if not metadata_path.exists() or not matches_path.exists():
        return 0
    with open(metadata_path, newline="", encoding="utf-8-sig") as f:
        meta_by_file = {row["filename"]: row for row in csv.DictReader(f)}
    with open(matches_path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    repaired = 0
    for i, row in enumerate(rows):
        if row.get("api_endpoint") == VERIFY_ENDPOINT and not row.get("match_id"):
            rows[i] = _complete_row(row, meta_by_file.get(row["filename"], {}))
            repaired += 1
    if repaired:
        store.write_matches(rows)
    metadata_capture.insert_match_rows([r for r in rows if r.get("api_endpoint") == VERIFY_ENDPOINT])
    return repaired


def run_local_verification():
    metadata_path = config.ARCHIVE_DIR / "metadata.csv"
    matches_path = config.ARCHIVE_DIR / "matches.csv"
    if not metadata_path.exists():
        return {"checked": 0, "confirmed": 0, "discrepancies": []}

    repaired = repair_verification_rows()
    if repaired:
        print(f"Completed {repaired} older verification rows in matches.csv and SQLite")

    with open(metadata_path, newline="", encoding="utf-8-sig") as f:
        pdf_rows = list(csv.DictReader(f))

    matches_by_file = {}
    if matches_path.exists():
        with open(matches_path, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                matches_by_file.setdefault(row["filename"], []).append(row)

    discrepancies = []
    confirmed = 0
    extra_page_rows = []

    for row in pdf_rows:
        fname = row["filename"]
        terms = matched_terms(row)
        path = config.term_pdf_dir(terms[0]) / fname
        if not path.exists():
            discrepancies.append({"filename": fname, "issue": "file missing on disk"})
            continue
        pages = pdf_utils.extract_pdf_pages_text(path.read_bytes())
        pages_by_term = {term: [] for term in terms}
        for page_num, page_text in pages:
            for term in text_utils.terms_in(page_text) & set(terms):
                pages_by_term[term].append(page_num)
        found_terms = text_utils.terms_in("\n".join(t for _, t in pages)) | {t for t in terms if pages_by_term[t]}
        if not set(terms) <= found_terms:
            alternate_text = _pdftotext(path)
            if alternate_text is not None:
                found_terms |= text_utils.terms_in(alternate_text)

        file_rows = matches_by_file.get(fname, [])
        all_found = True
        for term in terms:
            term_rows = [r for r in file_rows if (r.get("term") or terms[0]) == term]
            if term in found_terms and term_rows:
                existing_pages = {r["page_number"] for r in term_rows if r["page_number"]}
                for p in pages_by_term[term]:
                    if str(p) not in existing_pages:
                        extra_page_rows.append(_complete_row({
                            "filename": fname, "document_identifier": term_rows[0]["document_identifier"],
                            "collection": row["collection"], "congress": row["congress"], "page_number": p,
                            "matching_text_context": VERIFY_CONTEXT, "api_endpoint": VERIFY_ENDPOINT,
                            "term": term,
                        }, row))
            elif term in found_terms:
                all_found = False
                discrepancies.append({"filename": fname, "term": term,
                                      "issue": "term found in local PDF extraction but no matches.csv row exists"})
            else:
                all_found = False
                discrepancies.append({"filename": fname, "term": term,
                                      "issue": "term NOT found via local PDF re-extraction despite API-driven match "
                                               "(likely image/OCR-only PDF or extraction limitation)"})
        confirmed += int(all_found)

    if extra_page_rows:
        store.upsert_matches(extra_page_rows)
        metadata_capture.insert_match_rows(extra_page_rows)

    return {"checked": len(pdf_rows), "confirmed": confirmed, "discrepancies": discrepancies}
