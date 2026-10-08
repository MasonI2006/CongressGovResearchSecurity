"""PDF download, hashing, and text extraction utilities."""
import hashlib
import shutil
import subprocess
from io import BytesIO

from pypdf import PdfReader


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

