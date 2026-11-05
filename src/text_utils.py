"""Text extraction and term-matching helpers (matching rules live in search_spec)."""
import warnings

from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

from . import config
from .search_spec import normalize_whitespace  # noqa: F401  (re-exported)

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    return soup.get_text(separator=" ")


def find_matches(text: str):
    """Occurrences of the active search's terms in text, tolerant of line/page
    breaks between words. Returns dicts: term, start, end, context, surface."""
    return config.SEARCH.find_matches(text)


def terms_in(text: str) -> set:
    """Which of the active search's terms occur in text."""
    return config.SEARCH.terms_in(text)
