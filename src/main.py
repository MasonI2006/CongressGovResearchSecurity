"""Orchestrator: run every collection crawler for the configured congresses,
then run the website-search verification pass and local PDF verification pass,
then emit README.txt with full run statistics.

Usage:
    python -m src.main [--collections bill,amendment,...] [--congresses 119]
    python -m src.main --congresses 110-118      # a range
    python -m src.main --congresses 115,117-119  # a list mixing numbers and ranges
"""
import argparse
import datetime
import sys
import time

from . import client, config, crawlers, reporting, store, validate, verify, web_search

ALL_COLLECTIONS = [
    "bill", "amendment", "committee-report", "committee-print", "hearing",
    "committee-meeting", "congressional-record", "bound-congressional-record", "crs-report",
]

CRAWLER_FOR = {
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--collections", default=",".join(ALL_COLLECTIONS))
    ap.add_argument("--congresses", default=",".join(str(c) for c in config.CONGRESSES),
                    help='Congress numbers and/or ranges, e.g. "119", "117,119", "110-118"')
    ap.add_argument("--skip-website-search", action="store_true")
    ap.add_argument("--skip-verification", action="store_true")
    args = ap.parse_args()

    collections = [c.strip() for c in args.collections.split(",") if c.strip()]
    try:
        congresses = config.parse_congresses(args.congresses)
    except ValueError:
        ap.error(f'invalid --congresses {args.congresses!r}; use e.g. "119", "117,119", or "110-118"')
    unknown = [c for c in collections if c not in CRAWLER_FOR]
    if unknown:
        ap.error(f"unknown collection(s): {', '.join(unknown)}; choose from {', '.join(ALL_COLLECTIONS)}")

    start_time = datetime.datetime.now()
    print(f"=== Research Security archive run started {start_time.isoformat()} ===")
    print(f"Congresses: {congresses}")
    print(f"Collections: {collections}")

    total_matches = 0
    for congress in congresses:
        for coll in collections:
            fn = CRAWLER_FOR[coll]
            print(f"\n--- {coll} (congress {congress}) ---")
            t0 = time.time()
            try:
                matched = fn(congress)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                print(f"ERROR in {coll} for congress {congress}: {client.redact_text(str(e))}", file=sys.stderr)
                matched = 0
            total_matches += matched
            store.flush()
            dt = time.time() - t0
            print(f"{coll}/{congress}: {matched} matched documents in {dt:.0f}s "
                  f"(api_calls so far: {client.STATS['api_calls']}, cache_hits: {client.STATS['cache_hits']})")

    website_results, website_counts = [], {}
    website_ran = False
    if not args.skip_website_search:
        website_ran = True
        print("\n--- congress.gov website full-text search verification pass ---")
        for congress in congresses:
            try:
                res, cnt = web_search.run_website_search(congress)
                website_results.extend(res)
                website_counts[congress] = cnt
                print(f"Website search (congress {congress}): {cnt} reported results, {len(res)} scraped")
            except Exception as e:
                print(f"Website search failed (congress {congress}): {e!r}", file=sys.stderr)
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
    print_summary_table(congresses, collections)
    print(f"\n=== Done in {(end_time - start_time)} ===")


def print_summary_table(congresses, collections):
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
