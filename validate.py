"""Check a saved search's archive: python validate.py [--name NAME]

With only one saved search, --name can be left out."""
import argparse

from src import config
from src.search_spec import SearchSpec


def main():
    ap = argparse.ArgumentParser(description="Validate a saved search's PDFs against its metadata.")
    ap.add_argument("--name", help="search to validate (see: python -m src.main --list)")
    args = ap.parse_args()
    names = sorted(p.name for p in config.SEARCHES_DIR.glob("*") if (p / "search.json").is_file())
    name = args.name or (names[0] if len(names) == 1 else None)
    if not name:
        ap.error("choose a search with --name: " + (", ".join(names) or "no saved searches found"))
    if name not in names:
        ap.error(f"no saved search named {name!r}; saved searches: {', '.join(names) or 'none'}")
    config.activate_search(SearchSpec.load(config.search_dir(name)))
    from src import validate
    return 0 if validate.validate_archive()["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
