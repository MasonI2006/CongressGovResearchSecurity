"""Validate downloaded PDFs against CSV and sidecar metadata."""
import csv
import hashlib
from pathlib import Path

from . import config


def validate_archive():
    pdf_dir = config.PDF_DIR
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

    metadata_by_name = {}
    for row in metadata_rows:
        metadata_by_name.setdefault(row.get("filename", ""), []).append(row)
    pdf_files = sorted(pdf_dir.glob("*.pdf")) if pdf_dir.exists() else []

    for pdf_path in pdf_files:
        actual_sha = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
        rows = metadata_by_name.get(pdf_path.name, [])
        if not rows:
            problems.append(f"{pdf_path.name}: missing metadata row")
            continue
        if not any((row.get("pdf_sha256") or row.get("sha256")) == actual_sha for row in rows):
            problems.append(f"{pdf_path.name}: SHA-256 differs from metadata")
        sidecar_paths = {row.get("sidecar_path") for row in rows if row.get("sidecar_path")}
        existing_sidecars = [config.ROOT / p for p in sidecar_paths if (config.ROOT / p).is_file()]
        if not existing_sidecars:
            problems.append(f"{pdf_path.name}: missing metadata sidecar")
        match_exists = any(
            row.get("filename") == pdf_path.name or row.get("pdf_sha256") == actual_sha
            for row in match_rows
        )
        if not match_exists:
            problems.append(f"{pdf_path.name}: missing match record")

    lines = [
        "Congress.gov Research Security Archive Validation",
        f"PDF files checked: {len(pdf_files)}",
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
    config.ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    (config.ARCHIVE_DIR / "validation_report.txt").write_text(report, encoding="utf-8")
    print(report, end="")
    return {"pdf_count": len(pdf_files), "metadata_rows": len(metadata_rows),
            "match_rows": len(match_rows), "problems": problems, "passed": not problems}


def main():
    result = validate_archive()
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
