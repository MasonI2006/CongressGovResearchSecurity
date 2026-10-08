"""Text extraction and phrase-matching utilities."""
import re
import warnings

from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

_WORD_RE = re.compile(r"research\s+security", re.IGNORECASE)


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    text = soup.get_text(separator=" ")
    return text


def normalize_whitespace(text: str) -> str:
    """Collapse all whitespace (including newlines/hyphenated line-wraps) so that
    a phrase split across a line/page boundary is still detected."""
    # Join hyphenated line breaks: "research-\nsecurity" -> "researchsecurity" is wrong;
    # instead just collapse whitespace/newlines to single spaces so "research\nsecurity"
    # becomes "research security" and still matches.
    return re.sub(r"\s+", " ", text)


def find_matches(text: str, context_chars: int = 220):
    """Find all case-insensitive occurrences of "research security" in text,
    tolerant of line/page-boundary whitespace between the two words.

    Returns list of dicts: {"start": int, "end": int, "context": str}
    """
    if not text:
        return []
    norm = normalize_whitespace(text)
    matches = []
    for m in _WORD_RE.finditer(norm):
        start = max(0, m.start() - context_chars)
        end = min(len(norm), m.end() + context_chars)
        context = norm[start:end].strip()
        matches.append({"start": m.start(), "end": m.end(), "context": context})
    return matches


def contains_phrase(text: str) -> bool:
    if not text:
        return False
    return _WORD_RE.search(normalize_whitespace(text)) is not None
