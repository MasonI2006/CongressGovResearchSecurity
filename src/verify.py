"""Local verification pass: re-extract text from every downloaded PDF and
re-search for the phrase, cross-checking against matches.csv. Flags PDFs where
extraction disagrees with the API-driven search that originally flagged them,
and back-fills page numbers for matches that were originally found via an
HTML/XML text source (which has no page concept).
"""
import csv
import shutil
import subprocess

from . import config, pdf_utils, text_utils


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


def run_local_verification():
    metadata_path = config.ARCHIVE_DIR / "metadata.csv"
    matches_path = config.ARCHIVE_DIR / "matches.csv"
    if not metadata_path.exists():
        return {"checked": 0, "confirmed": 0, "discrepancies": []}

    with open(metadata_path, newline="", encoding="utf-8") as f:
        pdf_rows = list(csv.DictReader(f))

    matches_by_file = {}
    if matches_path.exists():
        with open(matches_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                matches_by_file.setdefault(row["filename"], []).append(row)

    discrepancies = []
    confirmed = 0
    extra_page_rows = []

    for row in pdf_rows:
        fname = row["filename"]
        path = config.PDF_DIR / fname
        if not path.exists():
            discrepancies.append({"filename": fname, "issue": "file missing on disk"})
            continue
        data = path.read_bytes()
        pages = pdf_utils.extract_pdf_pages_text(data)
        found_pages = []
        for page_num, page_text in pages:
            if text_utils.contains_phrase(page_text):
                found_pages.append(page_num)
        full_text = "\n".join(t for _, t in pages)
        found_in_pdf = bool(found_pages) or text_utils.contains_phrase(full_text)
        if not found_in_pdf:
            alternate_text = _pdftotext(path)
            found_in_pdf = alternate_text is not None and text_utils.contains_phrase(alternate_text)

        had_match_row = fname in matches_by_file
        if found_in_pdf and had_match_row:
            confirmed += 1
            existing_pages = {r["page_number"] for r in matches_by_file[fname] if r["page_number"]}
            for p in found_pages:
                if str(p) not in existing_pages:
                    extra_page_rows.append({
                        "filename": fname, "document_identifier": matches_by_file[fname][0]["document_identifier"],
                        "collection": row["collection"], "congress": row["congress"], "page_number": p,
                        "matching_text_context": "(confirmed via local PDF re-extraction pass)",
                        "char_location": "", "api_endpoint": "local_pdf_verification",
                    })
        elif found_in_pdf and not had_match_row:
            discrepancies.append({"filename": fname, "issue": "phrase found in local PDF extraction but no matches.csv row exists"})
        elif not found_in_pdf and had_match_row:
            discrepancies.append({"filename": fname, "issue": "phrase NOT found via local PDF re-extraction despite API-driven match (likely image/OCR-only PDF or extraction limitation)"})

    if extra_page_rows:
        with open(matches_path, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["filename", "document_identifier", "collection", "congress",
                                               "page_number", "matching_text_context", "char_location", "api_endpoint"])
            for r in extra_page_rows:
                w.writerow(r)

    return {"checked": len(pdf_rows), "confirmed": confirmed, "discrepancies": discrepancies}
