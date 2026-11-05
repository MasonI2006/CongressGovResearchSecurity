"""Validate a search's downloaded PDFs against its CSV and sidecar metadata.

Every metadata row must have its PDF (with the recorded SHA-256) in the folder
of each term it matched, a sidecar, and at least one match record; every PDF on
disk must have a metadata row.
"""
import csv
import hashlib
import json

from . import config


def _terms_of(row):
    try:
        terms = json.loads(row.get("matched_terms") or "[]")
    except json.JSONDecodeError:
        terms = []
    return [t for t in terms if t in config.SEARCH.terms] or [config.SEARCH.terms[0]]


def validate_archive():
    metadata_path = config.ARCHIVE_DIR / "metadata.csv"
    matches_path = config.ARCHIVE_DIR / "matches.csv"
    metadata_rows = []
    match_rows = []
    problems = []

    if metadata_path.exists():
        with metadata_path.open(newline="", encoding="utf-8-sig") as stream:
            metadata_rows = list(csv.DictReader(stream))
    else:
        problems.append("metadata.csv is missing")
    if matches_path.exists():
        with matches_path.open(newline="", encoding="utf-8-sig") as stream:
            match_rows = list(csv.DictReader(stream))
    else:
        problems.append("matches.csv is missing")

    matched = set()  # (filename or sha256, term) pairs that have a match record
    for row in match_rows:
        term = row.get("term") or config.SEARCH.terms[0]
        matched.add((row.get("filename"), term))
        matched.add((row.get("pdf_sha256"), term))
    expected = {}  # PDF path -> metadata row
    for row in metadata_rows:
        name = row.get("filename", "")
        for term in _terms_of(row):
            expected[config.term_pdf_dir(term) / name] = row
            if (name, term) not in matched and ((row.get("pdf_sha256") or row.get("sha256")), term) not in matched:
                problems.append(f"{name}: missing match record for term {term!r}")
        sidecar = row.get("sidecar_path")
        if not sidecar or not (config.ARCHIVE_DIR / sidecar).is_file():
            problems.append(f"{name}: missing metadata sidecar")

    on_disk = set()
    for pdf_dir in config.all_pdf_dirs():
        if pdf_dir.exists():
            on_disk.update(pdf_dir.glob("*.pdf"))
    for path in sorted(on_disk - set(expected)):
        problems.append(f"{path.relative_to(config.ARCHIVE_DIR)}: PDF has no metadata row for this term folder")
    checked = 0
    for path, row in sorted(expected.items()):
        rel = path.relative_to(config.ARCHIVE_DIR)
        if not path.exists():
            problems.append(f"{rel}: PDF listed in metadata.csv is missing")
            continue
        checked += 1
        if hashlib.sha256(path.read_bytes()).hexdigest() != (row.get("pdf_sha256") or row.get("sha256")):
            problems.append(f"{rel}: SHA-256 differs from metadata")

    lines = [
        f"Congress.gov Term Search Validation: {config.SEARCH.name}",
        f"Terms: {', '.join(config.SEARCH.terms)}",
        f"PDF files checked: {checked}",
        f"Metadata rows: {len(metadata_rows)}",
        f"Match rows: {len(match_rows)}",
        f"Gaps found: {len(problems)}",
        f"Result: {'PASS' if not problems else 'FAIL'}",
    ]
    if problems:
        lines.append("Gaps:")
        lines.extend(f"- {problem}" for problem in problems)
    else:
        lines.append("Every PDF has a metadata row, a matching SHA-256, a sidecar, and a match record.")
    report = "\n".join(lines) + "\n"
    (config.ARCHIVE_DIR / "validation_report.txt").write_text(report, encoding="utf-8")
    print(report, end="")
    return {"pdf_count": checked, "metadata_rows": len(metadata_rows),
            "match_rows": len(match_rows), "problems": problems, "passed": not problems}
