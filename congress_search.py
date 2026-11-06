"""Search Congress.gov documents for your own words or phrases, download the PDF
of every matching document, and record metadata for each match.

Examples:
    python congress_search.py --term "artificial intelligence" --congresses 118 --collections committee-print
    python congress_search.py --name chips --term "CHIPS Act" --term "semiconductor*" --congresses 117-118
    python congress_search.py --name china --terms-file terms.txt --congresses 115,117-118
    python congress_search.py --name chips --congresses 116     # extend a saved search
    python congress_search.py --list                            # show saved searches
    python congress_search.py --name chips --validate           # check a search's files

Before searching, it prints a rough time estimate and asks whether to start
(--estimate only shows the estimate; --yes starts without asking).

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
    other.add_argument("--estimate", action="store_true",
                       help="show how long the search would take, then exit without searching")
    other.add_argument("-y", "--yes", action="store_true",
                       help="start without asking for confirmation after the time estimate")
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
    return spec  # new search: saved once the user confirms


def validate_only(args, ap):
    names = saved_search_names()
    name = args.name or (names[0] if len(names) == 1 else None)
    if not name:
        ap.error("choose a search to validate with --name: " + (", ".join(names) or "no saved searches found"))
    if name not in names:
        ap.error(f"no saved search named {name!r}; saved searches: {', '.join(names) or 'none'}")
    core.activate_search(core.SearchSpec.load(core.search_dir(name)))
    return 0 if archive.validate_archive()["passed"] else 1


def describe_duration(seconds):
    if seconds < 90:
        return "about a minute" if seconds >= 30 else "under a minute"
    minutes = seconds / 60
    if minutes < 90:
        return f"about {round(minutes / 5) * 5 or 5} minutes" if minutes >= 10 else f"about {round(minutes)} minutes"
    hours = minutes / 60
    if hours < 48:
        return f"about {hours:.0f} hours" if hours >= 10 else f"about {hours:.1f} hours"
    return f"about {hours / 24:.1f} days ({hours:.0f} hours)"


def show_estimate(congresses, collections):
    """Print a rough time estimate for the run and return it in seconds."""
    print("Estimating how long this will take (checking how many documents each collection has)...")
    by_congress, by_collection = {}, {}
    for congress in congresses:
        for coll in collections:
            print(f"  checking {coll} for the {core.congress_ordinal(congress)} Congress...", end="\r", flush=True)
            seconds, _items = crawlers.estimate(coll, congress)
            by_congress[congress] = by_congress.get(congress, 0) + seconds
            by_collection[coll] = by_collection.get(coll, 0) + seconds
    total = sum(by_congress.values())
    print(" " * 79, end="\r")
    print("\nEstimated time (rough):")
    if len(congresses) > 1:
        for congress, seconds in by_congress.items():
            print(f"  {core.congress_ordinal(congress)} Congress: {describe_duration(seconds)}")
    if len(collections) > 1:
        slowest = sorted(by_collection.items(), key=lambda kv: -kv[1])[:4]
        print("  Slowest collections: " + ", ".join(f"{c} ({describe_duration(s)})" for c, s in slowest if s >= 60))
    print(f"  Total: {describe_duration(total)}")
    print("  Based on timings from a full 119th-Congress run. Items this search already finished are skipped, and\n"
          "  documents already in the download cache are counted as quick. Network speed and how much text the\n"
          "  API has (less for older Congresses) can change this a lot. Interrupted runs resume where they stopped.")
    return total


def confirm(args, ap):
    """Ask whether to start. Returns True to go ahead."""
    if args.yes:
        return True
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("\nOutput is going to a file, so there's no way to answer a question. "
              "Run the same command with --yes to start.")
        return False
    try:
        answer = input("\nStart the search? [y/N] ").strip().lower()
    except EOFError:
        answer = ""
    return answer in ("y", "yes")


def run(spec, congresses, collections, skip_verification):
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
    failed_downloads = core.failed_download_count()
    if failed_downloads:
        print(f"\n{failed_downloads} document(s) couldn't be downloaded. Their links are in "
              f"{core.failed_downloads_path()} for downloading by hand.")
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
    is_new = not (core.search_dir(spec.name) / "search.json").is_file()
    if not is_new:  # load its progress so finished items aren't counted
        core.activate_search(spec)
        core.init()
    show_estimate(congresses, collections)
    if args.estimate:
        return 0
    if not confirm(args, ap):
        print("Not started.")
        return 0
    if is_new:
        spec.save(core.search_dir(spec.name))
        core.activate_search(spec)
        core.init()
    run(spec, congresses, collections, args.skip_verification)
    return 0


if __name__ == "__main__":
    sys.exit(main())
