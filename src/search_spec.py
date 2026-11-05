"""What a search looks for: its terms and matching options.

A search is saved as search.json in its folder so later runs (resuming, or
adding Congresses) use exactly the same terms. Progress tracking is only valid
for the terms it was recorded with, so changing the terms of an existing
search is refused; start a new search name instead.

Matching rules
- Default: case-insensitive, whole words, any whitespace (including line and
  page breaks) between words. "AI" does not match inside "said".
- "*" is a wildcard for any letters/digits inside a word: "secur*" matches
  "secure", "security", "securities".
- regex=True: each term is a Python regular expression, used as written.
  Text is whitespace-normalized first, so line breaks appear as single spaces.
- case_sensitive=True: letter case must match exactly (e.g. "CHIPS" vs "chips").
"""
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")
CONTEXT_CHARS = 220


class SearchSpecError(ValueError):
    pass


def slugify(text: str) -> str:
    slug = text.lower().replace("*", "-wild")
    slug = re.sub(r"[^a-z0-9]+", "_", slug).strip("_")
    return slug[:60].strip("_") or "term"


def _is_word_edge(ch: str) -> bool:
    return ch == "*" or ch.isalnum() or ch == "_"


def phrase_pattern(term: str) -> str:
    """Regex for a plain phrase: words joined by flexible whitespace, '*' as an
    in-word wildcard, whole-word boundaries where the phrase edge is a word char."""
    words = term.split()
    body = r"\s+".join(r"\w*".join(re.escape(part) for part in word.split("*")) for word in words)
    start = r"(?<!\w)" if _is_word_edge(term.strip()[0]) else ""
    end = r"(?!\w)" if _is_word_edge(term.strip()[-1]) else ""
    return start + body + end


def normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text)


@dataclass
class SearchSpec:
    name: str
    terms: list            # list of term strings, in the user's order
    regex: bool = False
    case_sensitive: bool = False
    folders: dict = field(default_factory=dict)   # term -> per-term folder name
    created_at: str = ""

    def __post_init__(self):
        if not NAME_RE.match(self.name or ""):
            raise SearchSpecError(
                f"invalid search name {self.name!r}: use letters, digits, '_' or '-' (max 80 characters)")
        cleaned = []
        for term in self.terms:
            term = (term or "").strip()
            if term and term not in cleaned:
                cleaned.append(term)
        if not cleaned:
            raise SearchSpecError("no search terms given")
        self.terms = cleaned
        flags = 0 if self.case_sensitive else re.IGNORECASE
        self._compiled = []
        for term in self.terms:
            if not self.regex and not term.replace("*", "").strip():
                raise SearchSpecError(f"term {term!r} has no letters or digits")
            pattern = term if self.regex else phrase_pattern(term)
            try:
                self._compiled.append((term, re.compile(pattern, flags)))
            except re.error as e:
                raise SearchSpecError(f"term {term!r} is not a valid regular expression: {e}") from None
        # Stable, unique folder names for multi-term searches.
        used = set(self.folders.values())
        for term in self.terms:
            if term in self.folders:
                continue
            base = slugify(term)
            folder, n = base, 2
            while folder in used:
                folder, n = f"{base}_{n}", n + 1
            self.folders[term] = folder
            used.add(folder)
        self.folders = {t: self.folders[t] for t in self.terms}

    @property
    def multi_term(self) -> bool:
        return len(self.terms) > 1

    def same_search(self, other: "SearchSpec") -> bool:
        return (self.terms == other.terms and self.regex == other.regex
                and self.case_sensitive == other.case_sensitive)

    def describe(self) -> str:
        mode = "regular expressions" if self.regex else "phrases (whole words, * wildcard)"
        case = "case-sensitive" if self.case_sensitive else "case-insensitive"
        return f"{len(self.terms)} term(s), {mode}, {case}"

    def find_matches(self, text: str):
        """All occurrences of every term in the text, ordered by position.
        Returns dicts: term, start, end, context, surface (the matched text)."""
        if not text:
            return []
        norm = normalize_whitespace(text)
        found = []
        for term, regex in self._compiled:
            for m in regex.finditer(norm):
                if m.end() == m.start():
                    continue
                start = max(0, m.start() - CONTEXT_CHARS)
                end = min(len(norm), m.end() + CONTEXT_CHARS)
                found.append({"term": term, "start": m.start(), "end": m.end(),
                              "context": norm[start:end].strip(), "surface": m.group(0)})
        found.sort(key=lambda item: (item["start"], self.terms.index(item["term"])))
        return found

    def terms_in(self, text: str) -> set:
        if not text:
            return set()
        norm = normalize_whitespace(text)
        return {term for term, regex in self._compiled if regex.search(norm)}

    # --- persistence -------------------------------------------------------
    def to_json(self) -> dict:
        return {"name": self.name, "terms": self.terms, "regex": self.regex,
                "case_sensitive": self.case_sensitive, "term_folders": self.folders,
                "created_at": self.created_at}

    def save(self, folder: Path):
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "search.json").write_text(json.dumps(self.to_json(), indent=2, ensure_ascii=False) + "\n",
                                            encoding="utf-8")

    @classmethod
    def load(cls, folder: Path) -> "SearchSpec":
        data = json.loads((folder / "search.json").read_text(encoding="utf-8"))
        return cls(name=data["name"], terms=data["terms"], regex=data.get("regex", False),
                   case_sensitive=data.get("case_sensitive", False),
                   folders=dict(data.get("term_folders") or {}), created_at=data.get("created_at", ""))


def read_terms_file(path: Path) -> list:
    """One term per line; blank lines and lines starting with '#' are ignored."""
    try:
        lines = Path(path).read_text(encoding="utf-8-sig").splitlines()
    except OSError as e:
        raise SearchSpecError(f"cannot read terms file {path}: {e}") from None
    return [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]
