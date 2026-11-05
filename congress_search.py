"""Search Congress.gov documents for your own words or phrases, download the PDF
of every matching document, and record metadata for each match.

Examples:
    python congress_search.py --term "research security" --congresses 118
    python congress_search.py --name chips --term "CHIPS Act" --term "semiconductor*" --congresses 117-118
    python congress_search.py --name china --terms-file terms.txt --congresses 115,117-118
    python congress_search.py --name chips --congresses 116     # extend a saved search
    python congress_search.py --list                            # show saved searches
    python congress_search.py --name chips --validate           # check a search's files

Each search is saved under searches/<name>/ with its terms, so later runs with
the same --name resume it or extend it to more Congresses. See HowToUse.md.
"""
import argparse
import datetime
import sys
import time

import archive
import core
import crawlers


def build_parser():
    ap = argparse.ArgumentParser(
        prog="python congress_search.py",
        description="Search Congress.gov for words/phrases and archive the matching PDFs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=("Matching is case-insensitive and whole-word by default; '*' is a wildcard inside a word\n"
                "(secur* matches security, securities). Line breaks between words don't prevent a match.\n"
                "Collections: " + ", ".join(crawlers.CRAWLERS)))
    what = ap.add_argument_group("what to search for")
    what.add_argument("--term", action="append", default=[], metavar="TEXT",
                      help='a word or phrase to search for; repeat for several (e.g. --term "AI" --term "machine learning")')
    what.add_argument("--terms-file", metavar="FILE",
                      help="a text file with one term per line (blank lines and lines starting with # are ignored)")
    what.add_argument("--regex", action="store_true",
                      help="treat every term as a Python regular expression")
    what.add_argument("--case-sensitive", action="store_true",
                      help="require letter case to match exactly (default: case-insensitive)")
    what.add_argument("--name", help="name of this search; its results go in searches/<name>/. "
                                     "Required for several terms; defaults to the term for one term")
    where = ap.add_argument_group("where to search")
    where.add_argument("--congresses", default="119",
                       help='Congress numbers and/or ranges, e.g. "119", "117,119", "110-118" (default: %(default)s)')
    where.add_argument("--collections", default=",".join(crawlers.CRAWLERS),
                       help="comma-separated collections to search (default: all)")
    other = ap.add_argument_group("other")
    other.add_argument("--skip-verification", action="store_true",
                       help="skip re-reading downloaded PDFs to confirm the matches")
    other.add_argument("--list", action="store_true", help="list saved searches and exit")
    other.add_argument("--validate", action="store_true",
                       help="check a saved search's PDFs against its metadata and exit")
    return ap


def saved_search_names():
    return sorted(p.name for p in core.SEARCHES_DIR.glob("*") if (p / "search.json").is_file())


def list_searches():
    names = saved_search_names()
    if not names:
        print(f"No saved searches in {core.SEARCHES_DIR}")
    for name in names:
        try:
            spec = core.SearchSpec.load(core.search_dir(name))
        except (core.SearchSpecError, ValueError, KeyError) as e:
            print(f"{name}: unreadable search.json ({e})")
            continue
        print(f"{spec.name}: {spec.describe()}")
        for term in spec.terms:
            print(f"    {term}")


def resolve_search(args, ap):
    """Work out the search from --name/--term/--terms-file and any saved search.json."""
    terms = list(args.term)
    if args.terms_file:
        terms += core.read_terms_file(args.terms_file)
    name = args.name
    if not name:
        if len(terms) == 1:
            name = core.slugify(terms[0])
        elif terms:
            ap.error("give a search with several terms a name, e.g. --name my_topic")
        else:
            ap.error("say what to search for with --term or --terms-file "
                     "(or --name to continue a saved search; --list shows them)")
    folder = core.search_dir(name)
    saved = core.SearchSpec.load(folder) if (folder / "search.json").is_file() else None
    if saved and not terms:
        if args.regex or args.case_sensitive:
            ap.error(f"search {name!r} already exists; its matching options can't be changed. "
                     "Start a new --name instead")
        return saved
    if not terms:
        ap.error(f"no saved search named {name!r}; give --term or --terms-file to create it")
    spec = core.SearchSpec(name=name, terms=terms, regex=args.regex, case_sensitive=args.case_sensitive)
    if saved:
        if not saved.same_search(spec):
            ap.error(
                f"search {name!r} already exists with different terms or options:\n"
                f"  saved: {saved.terms} ({saved.describe()})\n"
                f"  given: {spec.terms} ({spec.describe()})\n"
                "Its progress only applies to the saved terms. Use a new --name for a different search "
                f"(the shared download cache keeps it fast), or run with just --name {name} to continue it.")
        return saved
    spec.save(folder)
    return spec


def validate_only(args, ap):
    names = saved_search_names()
    name = args.name or (names[0] if len(names) == 1 else None)
    if not name:
        ap.error("choose a search to validate with --name: " + (", ".join(names) or "no saved searches found"))
    if name not in names:
        ap.error(f"no saved search named {name!r}; saved searches: {', '.join(names) or 'none'}")
    core.activate_search(core.SearchSpec.load(core.search_dir(name)))
    return 0 if archive.validate_archive()["passed"] else 1


def run(spec, congresses, collections, skip_verification):
    core.activate_search(spec)
    core.init()

    start_time = datetime.datetime.now()
    print(f"=== Congress.gov term search '{spec.name}' started {start_time.isoformat()} ===")
    print(f"Output folder: {core.ARCHIVE_DIR}")
    print(f"Terms ({spec.describe()}):")
    for term in spec.terms:
        print(f"  {term}")
    print(f"Congresses: {congresses}")
    print(f"Collections: {collections}")

    for congress in congresses:
        for coll in collections:
            print(f"\n--- {coll} (congress {congress}) ---")
            t0 = time.time()
            try:
                matched = crawlers.CRAWLERS[coll](congress)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                print(f"ERROR in {coll} for congress {congress}: {core.redact_text(str(e))}", file=sys.stderr)
                matched = 0
            core.flush()
            print(f"{coll}/{congress}: {matched} matched documents in {time.time() - t0:.0f}s "
                  f"(api_calls so far: {core.STATS['api_calls']}, cache_hits: {core.STATS['cache_hits']})")

    verification = {"checked": 0, "confirmed": 0, "discrepancies": []}
    if not skip_verification:
        print("\n--- Local PDF verification pass ---")
        verification = archive.run_local_verification()
        print(f"Checked {verification['checked']} PDFs; confirmed {verification['confirmed']}; "
              f"{len(verification['discrepancies'])} discrepancies")
        for d in verification["discrepancies"]:
            print(f"  {d['filename']}" + (f" [{d['term']}]" if d.get("term") else "") + f": {d['issue']}")

    archive.persist_collection_counters(core.COUNTERS)
    archive.write_term_views()
    print("\n--- Archive validation ---")
    validation = archive.validate_archive()
    failures = core.pending_match_failures()
    print(f"Matched documents not marked processed: {len(failures)}")
    for failure in failures:
        print(f"  {failure['collection']} | {failure['document_identifier']} | "
              f"{failure.get('error') or 'interrupted before completion'}")
    end_time = datetime.datetime.now()
    archive.write_readme(congresses, collections, start_time, end_time,
                         not skip_verification, verification, validation, failures)

    print("\nCongress | Collection | Records searched | Full text searched? | Matches | Unique PDFs")
    print("-" * 100)
    for congress in congresses:
        for coll in collections:
            c = core.COUNTERS.get((coll, congress), {"examined": 0, "full_text_searched": False,
                                                     "matches": 0, "unique_pdfs": set()})
            print(f"{congress} | {coll} | {c['examined']} | {c['full_text_searched']} | "
                  f"{c['matches']} | {len(c['unique_pdfs'])}")
    print(f"\nResults: {core.ARCHIVE_DIR}")
    print(f"=== Done in {(end_time - start_time)} ===")


def main():
    ap = build_parser()
    args = ap.parse_args()
    if args.list:
        list_searches()
        return 0
    if args.validate:
        return validate_only(args, ap)

    collections = [c.strip() for c in args.collections.split(",") if c.strip()]
    unknown = [c for c in collections if c not in crawlers.CRAWLERS]
    if unknown:
        ap.error(f"unknown collection(s): {', '.join(unknown)}; choose from {', '.join(crawlers.CRAWLERS)}")
    try:
        congresses = core.parse_congresses(args.congresses)
    except ValueError:
        ap.error(f'invalid --congresses {args.congresses!r}; use e.g. "119", "117,119", or "110-118"')
    try:
        spec = resolve_search(args, ap)
    except core.SearchSpecError as e:
        ap.error(str(e))
    run(spec, congresses, collections, args.skip_verification)
    return 0


if __name__ == "__main__":
    sys.exit(main())
