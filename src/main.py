"""Search Congress.gov documents for your own words or phrases, download the PDF
of every matching document, and record metadata for each match.

Examples:
    python -m src.main --term "research security" --congresses 118
    python -m src.main --name chips --term "CHIPS Act" --term "semiconductor*" --congresses 117-118
    python -m src.main --name china --terms-file terms.txt --congresses 115,117-118
    python -m src.main --name chips --congresses 116        # extend a saved search
    python -m src.main --list                               # show saved searches

Each search is saved under searches/<name>/ with its terms, so later runs with
the same --name resume it or extend it to more Congresses.
"""
import argparse
import datetime
import sys
import time

from . import config
from .search_spec import SearchSpec, SearchSpecError, read_terms_file, slugify

ALL_COLLECTIONS = [
    "bill", "amendment", "committee-report", "committee-print", "hearing",
    "committee-meeting", "congressional-record", "bound-congressional-record", "crs-report",
]


def build_parser():
    ap = argparse.ArgumentParser(
        prog="python -m src.main",
        description="Search Congress.gov for words/phrases and archive the matching PDFs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=("Matching is case-insensitive and whole-word by default; '*' is a wildcard inside a word\n"
                "(secur* matches security, securities). Line breaks between words don't prevent a match.\n"
                "Collections: " + ", ".join(ALL_COLLECTIONS)))
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
    where.add_argument("--congresses", default=",".join(str(c) for c in config.CONGRESSES),
                       help='Congress numbers and/or ranges, e.g. "119", "117,119", "110-118" (default: %(default)s)')
    where.add_argument("--collections", default=",".join(ALL_COLLECTIONS),
                       help="comma-separated collections to search (default: all)")
    ap.add_argument("--skip-website-search", action="store_true",
                    help="skip the Congress.gov website-search cross-check")
    ap.add_argument("--skip-verification", action="store_true",
                    help="skip re-reading downloaded PDFs to confirm the matches")
    ap.add_argument("--list", action="store_true", help="list saved searches and exit")
    return ap


def list_searches():
    folders = sorted(p for p in config.SEARCHES_DIR.glob("*") if (p / "search.json").is_file())
    if not folders:
        print(f"No saved searches in {config.SEARCHES_DIR}")
        return
    for folder in folders:
        try:
            spec = SearchSpec.load(folder)
            print(f"{spec.name}: {spec.describe()}")
            for term in spec.terms:
                print(f"    {term}")
        except (SearchSpecError, ValueError, KeyError) as e:
            print(f"{folder.name}: unreadable search.json ({e})")


def resolve_search(args, ap):
    """Work out the search from --name/--term/--terms-file and any saved search.json."""
    terms = list(args.term)
    if args.terms_file:
        terms += read_terms_file(args.terms_file)
    name = args.name
    if not name:
        if len(terms) == 1:
            name = slugify(terms[0])
        elif terms:
            ap.error("give a search with several terms a name, e.g. --name my_topic")
        else:
            ap.error("say what to search for with --term or --terms-file "
                     "(or --name to continue a saved search; --list shows them)")
    folder = config.search_dir(name)
    saved = SearchSpec.load(folder) if (folder / "search.json").is_file() else None
    if saved and not terms:
        if args.regex or args.case_sensitive:
            ap.error(f"search {name!r} already exists; its matching options can't be changed. "
                     "Start a new --name instead")
        return saved
    if not terms:
        ap.error(f"no saved search named {name!r}; give --term or --terms-file to create it")
    spec = SearchSpec(name=name, terms=terms, regex=args.regex, case_sensitive=args.case_sensitive)
    if saved:
        if not saved.same_search(spec):
            ap.error(
                f"search {name!r} already exists with different terms or options:\n"
                f"  saved: {saved.terms} ({saved.describe()})\n"
                f"  given: {spec.terms} ({spec.describe()})\n"
                "Its progress only applies to the saved terms. Use a new --name for a different search "
                f"(the shared API cache keeps it fast), or run with just --name {name} to continue it.")
        return saved
    spec.save(folder)
    return spec


def main():
    ap = build_parser()
    args = ap.parse_args()
    if args.list:
        list_searches()
        return

    collections = [c.strip() for c in args.collections.split(",") if c.strip()]
    unknown = [c for c in collections if c not in ALL_COLLECTIONS]
    if unknown:
        ap.error(f"unknown collection(s): {', '.join(unknown)}; choose from {', '.join(ALL_COLLECTIONS)}")
    try:
        congresses = config.parse_congresses(args.congresses)
    except ValueError:
        ap.error(f'invalid --congresses {args.congresses!r}; use e.g. "119", "117,119", or "110-118"')
    try:
        spec = resolve_search(args, ap)
    except SearchSpecError as e:
        ap.error(str(e))

    config.activate_search(spec)
    # Imported after the search is active: these modules work in its folder.
    from . import client, crawlers, reporting, store, validate, verify, web_search
    store.init()
    crawler_for = {
        "bill": crawlers.crawl_bills,
        "amendment": crawlers.crawl_amendments,
        "committee-report": crawlers.crawl_committee_reports,
        "committee-print": crawlers.crawl_committee_prints,
        "hearing": crawlers.crawl_hearings,
        "committee-meeting": crawlers.crawl_committee_meetings,
        "congressional-record": crawlers.crawl_daily_congressional_record,
        "bound-congressional-record": crawlers.crawl_bound_congressional_record,
        "crs-report": crawlers.crawl_crs_reports,
    }

    start_time = datetime.datetime.now()
    print(f"=== Congress.gov term search '{spec.name}' started {start_time.isoformat()} ===")
    print(f"Output folder: {config.ARCHIVE_DIR}")
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
                matched = crawler_for[coll](congress)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                print(f"ERROR in {coll} for congress {congress}: {client.redact_text(str(e))}", file=sys.stderr)
                matched = 0
            store.flush()
            dt = time.time() - t0
            print(f"{coll}/{congress}: {matched} matched documents in {dt:.0f}s "
                  f"(api_calls so far: {client.STATS['api_calls']}, cache_hits: {client.STATS['cache_hits']})")

    website_results, website_counts = [], {}
    website_ran = False
    if not args.skip_website_search:
        website_ran = True
        print("\n--- congress.gov website full-text search verification pass ---")
        for term in spec.terms:
            if not web_search.website_searchable(term):
                print(f"Website search skipped for {term!r} (regular expressions can't be sent to the website)")
                continue
            for congress in congresses:
                try:
                    res, cnt = web_search.run_website_search(term, congress)
                    website_results.extend(res)
                    website_counts[(term, congress)] = cnt
                    print(f"Website search ({term!r}, congress {congress}): {cnt} reported results, {len(res)} scraped")
                except Exception as e:
                    print(f"Website search failed ({term!r}, congress {congress}): {e!r}", file=sys.stderr)
        web_search.write_results(website_results)

    verification = {"checked": 0, "confirmed": 0, "discrepancies": []}
    pdf_verification_ran = False
    if not args.skip_verification:
        pdf_verification_ran = True
        print("\n--- Local PDF verification pass ---")
        verification = verify.run_local_verification()
        print(f"Checked {verification['checked']} PDFs; confirmed {verification['confirmed']}; "
              f"{len(verification['discrepancies'])} discrepancies")

    reporting.persist_collection_counters(store.COUNTERS)
    reporting.write_term_views()
    print("\n--- Archive validation ---")
    validation = validate.validate_archive()
    failures = store.pending_match_failures()
    print(f"Matched documents not marked processed: {len(failures)}")
    for failure in failures:
        print(f"  {failure['collection']} | {failure['document_identifier']} | "
              f"{failure.get('error') or 'interrupted before completion'}")
    end_time = datetime.datetime.now()
    reporting.write_readme(congresses, collections, start_time, end_time, website_ran,
                           website_counts, pdf_verification_ran, verification, validation, failures)
    print_summary_table(store, congresses, collections)
    print(f"\nResults: {config.ARCHIVE_DIR}")
    print(f"=== Done in {(end_time - start_time)} ===")


def print_summary_table(store, congresses, collections):
    print("\nCongress | Collection | Records searched | Full text searched? | Matches | Unique PDFs")
    print("-" * 100)
    for congress in congresses:
        for coll in collections:
            c = store.COUNTERS.get((coll, congress), {"examined": 0, "full_text_searched": False,
                                                      "matches": 0, "unique_pdfs": set()})
            print(f"{congress} | {coll} | {c['examined']} | {c['full_text_searched']} | "
                  f"{c['matches']} | {len(c['unique_pdfs'])}")


if __name__ == "__main__":
    main()
