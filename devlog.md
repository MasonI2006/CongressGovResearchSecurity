# Development Log: Congress.gov Research Security Archive

## Goal and scope
Build a downloader that enumerates Congress.gov API collections, searches available document text for the case-insensitive phrase `research security`, downloads responsive PDFs, and records metadata and occurrences. The configured run was limited to the **119th Congress**, at the user's request for a run that could finish in a single session; it is not a historical all-Congresses search.

The API credential is stored locally in `.env` and excluded by `.gitignore`. It is intentionally not reproduced here. The originally supplied credential was rejected by the API; a corrected credential was provided and successfully tested.

## Implemented
- Added a Python package under `src/` with modules for configuration, cached/rate-limited HTTP requests, text normalization and phrase matching, PDF extraction and SHA-256 hashing, result storage, collection crawlers, verification routines, and the command-line orchestrator.
- Added disk caching, retry/backoff, API pagination using a page size of 250, progress bars, and persisted processed-document/deduplication state for resuming runs.
- Implemented crawlers for bills, amendments, committee reports, committee prints, hearings, committee meetings and their meeting/witness documents, daily Congressional Record articles, bound Congressional Record, and CRS products.
- Searches prefer an available formatted text/HTML/XML source and otherwise extract text from PDFs. Matching is case-insensitive and whitespace-normalized so a line break between the words does not prevent a match. PDF text extraction records page numbers where available.
- Downloads matched PDFs, hashes their bytes with SHA-256, and writes `research_security_archive/metadata.csv`, `matches.csv`, and `PDFs/`.
- Added a best-effort Congress.gov website-search cross-check and a local PDF re-extraction verifier. These are implemented but have **not been run** for the completed crawl.
- Installed `cryptography` and `pikepdf` in the active Python 3.10 environment; both are now listed in `requirements.txt`.

## Run history
1. A committee-print smoke test succeeded and found two 119th-Congress prints containing the phrase; both PDFs were downloaded. During this test, missing titles and separately-arranged committee-print/report format entries were identified and corrected.
2. The first all-collection run processed the main legislative, report, print, and hearing collections, but encountered failures during committee meetings (missing PDF dependency) and Congressional Record text (control characters broke CSV output). The CRS format-key mismatch also caused an initial zero-result pass. These issues were subsequently addressed in code.
3. The resumed run completed on **2026-09-18**, with the log ending `Done in 10:28:03`. No Python crawler process was present when status was checked on 2026-10-01. The final run used `--skip-website-search --skip-verification`.

## Inline Metadata Update (2026-10-01)
- Completed the approved field-discovery smoke test first and updated `field_inventory.md`. Raw sample JSON is under `raw_api/`; a scan of raw JSON, cache, archive outputs, and run logs found no API-key material or `api_key=` query strings.
- Added inline API status/timestamp capture, URL/log redaction, collection-aware metadata normalization, write-once raw snapshots, per-PDF sidecars, SQLite persistence, extended metadata/match CSV columns, idempotent upserts, control-character cleanup, and delayed processed-state marking until persistence succeeds.
- Added `archive.sqlite` tables for documents, files, retrievals, matches, people, committees, and related items. Added `validate.py` and automatic end-of-run validation. README generation now reads persisted CSV/SQLite contents and labels optional verification passes as `ran` or `skipped`.
- Added configurable archive/state output roots so the short test could run in an isolated temporary directory without touching the existing archive, cache, or state.
- Isolated end-to-end test used one matched committee print and one matched bill. The final repeated test produced 2 PDFs, 2 metadata rows, and 2 match rows on both runs; the second run added no duplicate rows/files and left PDF hashes unchanged. Validation reported:

	```text
	PDF files checked: 2
	Metadata rows: 2
	Match rows: 2
	Gaps found: 0
	Result: PASS
	```

- The optional per-file SQLite `api_fields_json` addition was reverted as requested; normalized fields remain in `documents.api_fields_json`, sidecars, and metadata CSV. `python -m compileall src` passed after the revert and after the final `policyArea.name` shape adjustment.
- Expanded smoke selected known matches from all seven matched collections, including one committee meeting document and one witness statement; it also ran CRS detail normalization for `CRS-R49457` without creating a matched CRS PDF. It ran twice in separate processes: 8 PDFs, 8 metadata rows, and 103 match rows were unchanged on the second pass, with all PDF hashes unchanged.
- Per-collection sidecar/SQLite checks passed for bill, amendment, committee report, committee print, hearing, committee meeting, and daily Congressional Record. CRS was detail-only because the old archive contains no CRS match. The bill has sponsor `bioguideId` F000480, committee `systemCode` hssy00, `policyArea.name`, and text-version type/date in sidecar and SQLite.
- Expected null/empty fields observed: bill `publicLawNumber` null with empty `laws`, `relatedBills`, and `legislativeSubjects`; amendment `title`, `description`, `purpose`, and latest-action text null; hearing `associatedMeeting` null; committee-meeting `relatedItems` empty; CRS `congress` null and `relatedMaterials[0].title` null. No unexpected missing/null fields were found in the audited sample fields.
- pypdf initially failed to find the H.R. 477 phrase in local text extraction; Poppler `pdftotext` found it in the same PDF. Added a `pdftotext` fallback to local verification and crawl-time PDF extraction. The final 8-PDF local verification confirmed all 8 with zero discrepancies.
- Added the same Poppler fallback to crawl-time `extract_pdf_pages_text`: it runs when pypdf fails or extracts fewer than about 1,250 non-whitespace characters per page, and its output is used only when it is richer. A direct H.R. 477 check now finds the phrase on extracted page 4.
- The final smoke was repeated with temporary archive, state, and cache directories: 8 PDFs across all matched collections (including a meeting document and witness statement), plus CRS detail normalization. Both passes kept the same 8 PDFs and 103 initial phrase rows; PDF hashes were unchanged. Local verification confirmed 8/8 with zero discrepancies and added page-specific rows; final `validate.py` reported 8 PDFs, 8 metadata rows, 139 match rows, 0 gaps, `PASS`.
- Simulated a matched-document metadata write failure: the document appeared in the persisted pending-failure list with its error, remained unprocessed, then cleared from the failure list after a successful retry.
- Full `compileall src` passes after the crawl-time fallback and final configuration. Final source backup: `src_backup_2026-10-01_final-prep/` (29 files; no Git repository existed at the time — one was created later, see 2026-11-05).
- Verification timing sample: one Congress.gov website-search page took 0.09 seconds but returned no parsed results, so this is not a reliable full-search benchmark. The scraper is capped at 40 pages with a one-second inter-page delay; plan roughly 1-2 minutes plus network variability. Local PDF verification took 8.56 seconds for 8 PDFs; simple linear projection is about 4-5 minutes for 232, with several more minutes possible for large PDFs.
- Existing archive PDFs and `state/` remain intact; the CSV headers were restored to their original schemas and all 232 metadata rows/987 match rows remain. Earlier discovery calls used the shared API cache and may have added response entries; it was not cleared. The final broad smoke used `CONGRESS_API_CACHE_DIR` and did not use the shared cache.
- The v1 archive was moved to `research_security_archive_v1_backup/`; `cache/` and `state/` were cleared as approved, and `.env` was left untouched. The full crawl completed; see the run results below.

## Full Crawl Results (2026-10-01 to 2026-10-02)
- Run: 119th Congress, all configured collections, no verification skip flags; logged to `run3.log`. Run duration reported by the app: 15:59:58.
- Final `validate.py`: **PASS**, 240 PDFs, 240 metadata rows, 1,265 match rows, 0 gaps.
- Pending matched documents not marked processed: **0**. The run log contains no `ERROR in` or `WARN:` entries.
- Website-search verification ran, but the scraper parsed 0 results and reported no result count; treat it as an inconclusive cross-check, not a completeness guarantee.
- Local PDF verification checked 240, confirmed 237, and found 3 discrepancies (still unresolved as of 2026-11-05): `BILL-119-HR-1048-Reported_in_House.pdf`, `BILL-119-HR-8800-Engrossed_in_House.pdf`, and `BILL-119-HR-8800-Reported_in_House.pdf`. The API-formatted text matched, but neither pypdf nor Poppler pdftotext found the phrase in those PDFs. Their metadata reports a text layer; these need manual review as source-text/PDF-content or extraction discrepancies.

| Collection | Records/items examined | Phrase occurrences | Per-collection PDF associations |
|---|---:|---:|---:|
| Bills | 22,588 | 168 | 74 |
| Amendments | 6,862 | 49 | 14 |
| Committee reports | 990 | 71 | 17 |
| Committee prints | 78 | 2 | 2 |
| Hearings | 1,008 | 134 | 17 |
| Committee meetings | 32,029 document items | 561 | 98 |
| Daily Congressional Record | 7,315 | 27 | 23 |
| Bound Congressional Record | 0 | 0 | 0 |
| CRS products | 2,639 | 0 | 0 |

Per-collection PDF associations can sum to more than the 240 unique downloaded PDFs because hashes are deduplicated across API locations. `matches.csv` has 1,265 rows after local verification added page-confirmation rows.

### v1 Comparison
- v1 backup: 232 PDFs / 232 distinct SHA-256 hashes.
- New archive: 240 PDFs / 240 distinct SHA-256 hashes.
- v1-only PDF hashes: 0. New-only PDF hashes: 8. No stable-ID/SHA associations were removed.
- New stable IDs: `119-HR-10574`, `119-S-5526`, `119-SAMDT-6838`, `119-house-118242__MTG-119-HOUSE-118242-12-Witness_Truth_in_Testimony`, `119-house-118990__MTG-119-HOUSE-118990-1-Bills_and_Resolutions`, `119-house-119247__MTG-119-HOUSE-119247-0-Bills_and_Resolutions`, `119-senate-64619`, and `119-senate-64653`.
- Full comparison, including each new PDF's SHA-256, is in `research_security_archive/v1_comparison.txt`.
- Source backup: `src_backup_2026-10-01_final-prep/` (29 files). No Git repository was present at the time (one was created later; see 2026-11-05).

## v1 Baseline Counts
These are the original pre-inline-capture archive counts, retained for comparison. `matches.csv` has one row per recorded text occurrence; counts can exceed the number of matched documents.

| Collection | Records/items examined (run summary) | Occurrences in matches.csv | PDFs attributed in metadata.csv | Notes |
|---|---:|---:|---:|---|
| Bills | 21,946 | 165 | 72 | Searches available bill text versions. |
| Amendments | 6,787 | 48 | 13 | Coverage of text depends on what the API exposes for the amendment. |
| Committee reports | 948 | 71 | 17 | Uses report text endpoint and available formats. |
| Committee prints | 0 in final persisted counter | 2 | 2 | The smoke test found these; persisted counters were not restored for this collection on resume. |
| Hearings | 967 | 120 | 15 | Searches formatted transcript text when available. |
| Committee meetings | 31,881 document-processing attempts | 555 | 90 | Includes meeting/witness documents; this is not a count of distinct meetings. |
| Daily Congressional Record | 7,125 | 26 | 23 | Searches article text/PDFs; earlier first-run text processing failed on control characters and was rerun. |
| Bound Congressional Record | 0 | 0 | 0 | No records found for the selected recent Congress in the probed years. |
| CRS products | 2,593 | 0 | 0 | Filtered by publication date for the 119th Congress; the API does not assign a Congress number. |
| **Total currently in archive** | — | **987** | **232** | Metadata CSV has 232 unique-PDF rows; 232 PDF files are currently on disk. |

## Known limitations and outstanding work
- **The Congress.gov website-search cross-check is inconclusive.** It ran, but the HTML scraper parsed no results. The local PDF pass found three unresolved discrepancies listed above.
- The v1 `README.txt` is stale and remains only in `research_security_archive_v1_backup/`; the new archive README was generated from current persisted output.
- `committee-print`'s persisted run counter was missing after resume, even though its two rows and PDFs remain in the archive. Some per-collection counters count processed versions/documents rather than distinct parent records; interpret them accordingly.
- CRS reports are matched by publication date, not by Congress. Older CRS products updated during the 119th Congress may not be included by that date filter. API coverage/format availability can also limit searchable full text.
- House/Senate communications, nominations, and treaties were not searched because the API endpoints inspected did not expose their substantive full text/PDF corpus in the implemented pass. Historical coverage before the configured Congress was not attempted.
- OCR is not implemented. Image-only PDFs can therefore contain the phrase without either pypdf or pdftotext finding it. Some PDFs returned warnings or malformed-object messages from `pypdf`; these indicate file-structure issues and should be reviewed during the full local verification.
- The website search scraper is an undocumented HTML-UI probe, not a stable Congress.gov API; even when run, it should be treated as a cross-check rather than proof of exhaustiveness.
- The old v1 archive was not retroactively enriched; all new structured metadata is in the new archive. Historical Congresses other than the configured 119th were not searched.
- Downloaded response caches and the `.env` credential should remain local. `.gitignore` excludes `.env`, cache/state, and downloaded PDF files.

## How the pipeline works
1. `src/main.py` selects the 119th Congress and configured collections, then invokes each crawler.
2. A crawler paginates collection listings and fetches each item's detail or text endpoint to locate available text and PDF URLs.
3. The processor retrieves formatted text where possible, normalizes whitespace, and searches for the phrase. If a match is found, it downloads the unchanged PDF bytes and calls the inline metadata pipeline before marking that document processed.
4. The inline pipeline fetches only that match's detail and relevant sub-endpoints, writes sanitized raw JSON once, creates a metadata sidecar, upserts metadata/matches CSV rows and SQLite records, then returns to the processor.
5. API/text/PDF responses are cached under `cache/`; processed identifiers and collection counters are checkpointed under `state/` to support resuming. The configured archive/state root can be overridden for isolated tests.
6. The orchestrator generates README/validation reports from persisted outputs. Optional Congress.gov website search and local PDF verification are labeled accurately as ran or skipped.

## Main files
(Updated 2026-11-05 for user-defined search terms; see that section.)
- `src/config.py` — API configuration, Congress selection, request limits, shared cache path, and the active search's folders (`activate_search`, `term_dir`, `term_pdf_dir`).
- `src/search_spec.py` — a search's terms and matching options (phrase/wildcard/regex, case), matching, and `search.json` load/save.
- `src/client.py` — API and file HTTP access, cache, pagination, retry and throttling.
- `src/crawlers.py` — collection enumeration and endpoint-specific document discovery.
- `src/processor.py` — text/PDF matching and document handling.
- `src/metadata_capture.py` — matched-only API detail/sub-endpoint capture, raw snapshots, sidecars, and SQLite writes.
- `src/text_utils.py`, `src/pdf_utils.py` — HTML-to-text and term-matching wrappers; PDF text extraction/hash helpers.
- `src/store.py` — CSV output, deduplication and persisted run state.
- `src/reporting.py`, `src/validate.py`, `validate.py` — output-derived README, per-term CSV views, and per-search PDF archive validation (`python validate.py --name <search>`).
- `src/web_search.py`, `src/verify.py` — optional verification passes (both ran in the full crawl). `verify.py` also repairs incomplete verification rows.
- `src/main.py` — command line (`--term`, `--terms-file`, `--name`, `--regex`, `--case-sensitive`, `--list`), orchestration, and README/report generation.
- `searches/<name>/` — one folder per search: `search.json`, PDFs (or `terms/<term>/PDFs` for several terms), CSVs, SQLite, sidecars, `raw_api/`, `state/`, README. The original archive is `searches/research_security/`.
- `searches/<name>/raw_api/` — sanitized raw API snapshots; sidecars and SQLite reference these paths (relative to the search folder), so it is part of the archive.
- `searches/<name>/state/dedupe_state.json` — processed IDs, SHA-256→filename map, per-Congress counters, and finished-parent markers (`parents_done`); keep it so future Congress runs dedupe against the existing archive and skip finished items.
- `searches/research_security/run3.log` — log of the full 119th crawl that produced that archive (UTF-16).
- `cache/` — API/text/PDF response cache shared by all searches (git-ignored).

## Cleanup (2026-10-07)
In preparation for extending the crawler beyond the 119th Congress:
- Moved to `_to_delete/` for review: the three `src_backup_2026-10-01*` folders (two were identical to `src/`, the third predated the pdftotext fallback), the empty `.venv` (Python 3.12 with only pip; runs used system Python 3.10), the root `__pycache__/`, `research_security_archive_v1_backup/` (fully superseded; `v1_comparison.txt` is kept in the archive), and `run.log`/`run2.log`.
- Removed unused code: `main.write_readme` and `ENDPOINT_NOTE` (old v1 README writer, replaced by `reporting.write_readme`), `store.register_pdf`, `store.record_match`, `store._clean`, `pdf_utils.extract_pdf_full_text`, `metadata_capture._raw_reference`, and `text_utils.PHRASE`.
- `compileall src` passes and `validate.py` still reports PASS (240 PDFs, 0 gaps).
- `_to_delete/` was subsequently deleted by the user, so the source backups and `research_security_archive_v1_backup/` no longer exist; `research_security_archive/v1_comparison.txt` is the only remaining v1 record. `cache/` is also gone, so any re-crawl will re-fetch every API response (a repeat of the 119th Congress costs roughly 22,000+ API calls, since crawlers fetch each item's text endpoint before checking processed state).

## Multi-Congress Support (2026-10-07)
Goal: run everything the pipeline does for any selected Congress(es), not only the 119th.

**Selecting Congresses.** `--congresses` (and the `CONGRESSES` env var) now accept single numbers, lists, and ranges: `119`, `117,119`, `110-118`, `115,117-119`. Invalid input and unknown `--collections` names are rejected with a usage error. Congresses always run in ascending order.

**API probe of older Congresses (one call each).** Bills (110th text versions with Formatted Text + PDF), amendments (110th list; 115th text), committee meetings (114th), hearings (110th), committee reports (105th), committee prints (110th), and the daily Congressional Record list (5,880 issues, each tagged with `congress`) all use the same endpoints and shapes as the 119th, so their crawlers work unchanged. The Bound Congressional Record does not: list entries point to an API detail endpoint, and the detail returns only `congress`, `date`, `sessionNumber`, `volumeNumber`, `updateDate`, and `sections[name/startPage/endPage]`, with no text or PDF links. The old crawler would have sent each entry's API URL to the PDF downloader. In addition, `bound-congressional-record/2021` and `/2022` return a persistent HTTP 500 (`Object of type HttpResponse is not JSON serializable`).

**Code changes**
- `config.py`: `parse_congresses`, `congress_ordinal`, `congress_years` (first year = 1787 + 2n), and `congress_date_range` (Jan 3 starts from the 74th Congress on, Mar 4 before). New optional overrides `CONGRESS_API_CACHE_DIR` (the devlog previously referred to this, but it was not implemented) and `RESEARCH_SECURITY_RAW_DIR`, so isolated tests no longer write into the real `cache/` or `raw_api/`.
- `crawlers.py`: removed the hard-coded `CONGRESS_YEAR_RANGE` (117-119 only, which made CRS and bound-record crawls silently return 0 for any other Congress). CRS uses `congress_date_range`. Congress.gov URLs use correct ordinals (`101st`, `102nd`, `103rd`, not `101th`). The bound-record crawler is now a probe: it checks each session year, prints the entry count, warns (`WARN:`) if a text/format field ever appears, and logs API errors. It uses `max_attempts=2` so a persistent 500 costs about 4 s instead of about 3 min per year.
- `client.py`: `api_get` takes an optional `max_attempts` (default 8, unchanged).
- `store.py` / `processor.py`: run counters are keyed by `(collection, congress)` and persisted as `collection_counts_by_congress`. The old `collection_counts` block migrates to the 119th on first load, since it was produced only by 119th runs. Verified on a copy of `state/dedupe_state.json`: all nine collection counters migrated with identical examined/unique-PDF counts and survived a save/reload.
- `reporting.py`: new SQLite table `collection_congress_stats` (PK collection+congress); `collection_stats` now holds totals across Congresses. README shows per-Congress counters, all-Congress totals, CSV totals by Congress and collection, per-Congress website-search counts, and a bound-record limitation line.
- `web_search.py` / `main.py`: website-search results gain a `congress` column and are accumulated across Congresses, then written once. Previously each Congress overwrote the CSV.
- `metadata_capture.py`: new `matches.csv` rows fill the existing `congress` column (previously blank for crawl-time rows). `match_id` is unchanged, so upserts stay idempotent.

**Smoke test (isolated temp archive/state/cache/raw dirs; real archive untouched).** `--congresses 117 --collections committee-print,bound-congressional-record --skip-website-search`: 153 prints examined, 2 matched (`CPRT-117-HOUSE-47742`, `CPRT-117-HOUSE-50347`; 10 crawl-time occurrences, 15 match rows after local verification). Local verification confirmed 2/2. Validation PASS, 0 pending. Metadata `congress` = 117, and match rows carry 117. A follow-up `--congresses 117,114 --collections bound-congressional-record` probe reported 190 entries for 2015 and 3,237 for 2016 (metadata only), and API errors for 2021-2022, in 10 s total. The ad-hoc API probes for this work used the shared `cache/`, which now exists again with a few dozen entries.

**Notes for older runs**
- `state/dedupe_state.json` is shared, and every document ID except CRS embeds the Congress, so new Congresses don't collide with 119th records. CRS IDs (`CRS-<id>-v<n>`) are deduplicated across Congresses, which is correct because the publish-date windows don't overlap.
- The daily Congressional Record and CRS crawlers still page through their full listings once per Congress (about 24 and about 80 pages); after the first Congress in a run these are cache hits.
- API coverage likely thins for older Congresses (unverified; only the samples above were probed), so low counts there may reflect API text availability rather than absence of the phrase. Check each collection's `full_text_searched` flag and examined count in the README per Congress.

### API coverage by Congress (probe, 2026-10-07)
Sampled list counts plus the text formats of the first few items per collection (17 Congresses from the 81st to the 116th). These are samples, not exhaustive checks.

| Collection | Records from | Searchable text from | Notes |
|---|---|---|---|
| Bills | 90th (994), 93rd+ in full | 101st (Formatted Text only); 103rd+ with PDF | 93rd-100th sampled bills had no text. 101st-102nd samples had no PDF, so a match there would fail with "Matched document has no PDF format" and stay pending. |
| Amendments | 97th | 105th (HTML + PDF) | |
| Hearings | 104th (House), 105th (Senate) | same | |
| Committee reports | 104th | same | |
| Committee prints | 101st (1-2 per Congress until the 103rd) | same | |
| Committee meetings | 112th (House), Senate by the 116th (0 in the 114th) | same | |
| Daily Congressional Record | Oldest listed issue 2009-08-03 (111th, vol. 155) | same | |
| CRS products | Oldest publishDate on the last listing page: 2014-10-08 | same | Listing order not confirmed to be chronological. |

Practical floor: the **103rd Congress** for bills, and the **104th-105th** for most other collections.

## Fixes before older-Congress runs (2026-11-05)
**Git.** The project is now a Git repository (`main`, initial commit `821154b`). Tracked: `src/`, docs, `raw_api/`, archive CSVs/SQLite/sidecars/README, `state/dedupe_state.json`, and `run3.log`. Ignored: `.env`, `cache/`, and archive PDFs. Earlier entries saying "no Git repository" were true when written.

**1. CRS version in document IDs.** `crawl_crs_reports` read `version` from the CRS detail payload, but the detail calls it `currentVersion` (list entries call it `version`). Every CRS ID was therefore `CRS-<id>-v1`: all 2,639 processed CRS IDs in state end in `-v1`, while the listing shows reports at version 6. A revised report would have been skipped as already processed. IDs now use `currentVersion`, then `version`, then the list entry's `version`. No CRS PDFs exist, so no archive rows change. A 119th re-run will search CRS reports above version 1 again under their correct IDs.

**2. Complete verification rows.** `verify.py` appended page-confirmation rows to `matches.csv` with only 8 of the 14 columns: no `stable_id`, `pdf_sha256`, `match_id`, or `text_source_used`, and nothing in SQLite. That caused the CSV/SQLite gap (1,265 vs 1,012). Verification rows are now complete, get a deterministic `match_id`, are upserted into the CSV, and are inserted into SQLite (`metadata_capture.insert_match_rows`). `verify.repair_verification_rows()` completes older rows and runs at the start of every verification pass. Applied to the real archive: 253 rows repaired; CSV and SQLite now both have **1,265** matches; the 1,012 crawl-time rows are unchanged; `validate.py` PASS (240 PDFs, 0 gaps). A second repair pass changed nothing. `research_security_archive/README.txt` still shows 1,012 SQLite matches until the next run regenerates it.

**3. Skip finished items without refetching.** Crawlers fetched every item's text/detail endpoint before checking processed state, so a repeat run cost nearly as many API calls as the first. Now each parent record (bill, amendment, report part, print, hearing, meeting, daily CR issue, CRS report) is recorded in `state/dedupe_state.json` under `parents_done`, with the list entry's update date (`updateDateIncludingText` for bills, `updateDate` elsewhere), once all its documents are processed. Later runs skip it with no per-item calls while that date is unchanged; a changed date (e.g., a new bill text version) is re-checked. Parents with a fetch error or a matched document that failed persistence stay open. Existing 119th state has no markers yet, so the next 119th run still costs the full ~22,000+ calls once; later re-runs are cheap.

**Test (isolated temp archive/state/cache/raw dirs; real archive untouched).** `--congresses 117 --collections committee-print --skip-website-search`, twice:
- Pass 1: 153 prints, 2 matched, 310 API calls, 9 min 37 s; local verification confirmed 2/2; 15 match rows (5 from verification), all with `match_id`, all 15 in SQLite; validation PASS.
- Pass 2, with a new empty API cache so every non-skipped item would have cost a real call: **4 API calls** (list pages only), 3 s of crawling; still 15 match rows, no duplicates; validation PASS; 153 `parents_done` entries.
- Repair test on a copy of the real CSV/SQLite: 253 repaired, then 0 on the second run.

`compileall src` passes.

## User-defined search terms (2026-11-05)
Goal: make the tool usable by researchers for any words or phrases, not only "research security".

**Design (chosen with the user)**
- Terms: one `--term`, several `--term` flags, and/or `--terms-file` (one per line; blank lines and `#` comments ignored).
- Matching (`src/search_spec.py`): case-insensitive and whole-word by default; whitespace-tolerant between words (text is whitespace-normalized, so line/page breaks still match); `*` is an in-word wildcard (`\w*`). `--regex` treats terms as Python regexes. `--case-sensitive` applies to all terms. Zero-length regex matches are ignored; invalid regexes and terms with no letters/digits are rejected up front.
- Each search is named (`--name`; one-term searches default to the term's slug) and lives in `searches/<name>/` with its own `search.json`, PDFs, CSVs, SQLite, sidecars, `raw_api/`, and `state/`. All searches share `cache/`.
- Several terms: each term gets `terms/<folder>/` with its own `PDFs/` and filtered `metadata.csv` and `matches.csv` (rewritten at the end of each run from the search-wide files) plus `TERM.txt`. A PDF that matches two terms is copied into both folders. The search-wide `metadata.csv`, `matches.csv`, and SQLite stay the complete record. One-term searches keep `PDFs/` at the top of the search folder.
- Progress (`processed_doc_ids`, `parents_done`) only means "checked for these terms", so an existing search's terms/options can't be changed; the CLI refuses with an explanation and suggests a new `--name` (the shared cache keeps that cheap). `--name X` alone resumes or extends a saved search. `--list` shows saved searches.

**Data changes**
- `matches.csv` and SQLite `matches` gain a `term` column (SQLite column added automatically on older databases). `matched_surface_form` is now the text the term actually matched. `match_id` now includes the term (`store.match_id`), since two terms can match the same spot.
- `metadata.csv` gains `matched_terms` (JSON list; merged when a deduplicated PDF later matches another term). Sidecars gain `matched_terms`.
- Stored paths (sidecar, raw snapshot) are relative to the search folder, so a search folder can be zipped and shared.
- Verification checks each PDF for each term it was saved under and writes per-term page rows. Validation checks every term folder's copy, its SHA-256, a per-term match record, and the sidecar, and reports any PDF without a metadata row. The website cross-check runs per term and is skipped for regex terms. The README lists the terms/options and a per-term table.
- Env vars `RESEARCH_SECURITY_ARCHIVE_DIR`, `_STATE_DIR`, and `_RAW_DIR` are replaced by `CONGRESS_SEARCHES_DIR`; `CONGRESS_API_CACHE_DIR` is unchanged. The API key is now checked at the first API call, so `validate.py` and `--list` work without one. `store` no longer loads state at import; `main` calls `config.activate_search()` and `store.init()` first.

**Migration of the existing archive (one-off script, run once).** `research_security_archive/*`, `raw_api/`, `state/`, and `run3.log` moved to `searches/research_security/`; `search.json` = `["research security"]`, phrase mode, case-insensitive. The `research_security_archive/` prefix was removed from paths in `metadata.csv`, all 249 sidecars, and SQLite (`documents`, `files`, `retrievals`). All 1,265 match rows got `term` and recomputed `match_id`s (no collisions), applied identically in CSV and SQLite. All 240 metadata rows got `matched_terms`. Afterwards: `python validate.py` **PASS** (240 PDFs, 240 metadata rows, 1,265 match rows, 0 gaps); 0 of 240 metadata raw paths, 0 of 711 sidecar retrieval paths, and 0 SQLite retrieval paths are missing. `README.txt` and `website_search_results.csv` are from the 2026-10-02 run and refresh on the next run. `python -m src.main --term "research security"` resolves to this search (same slug and options), so it continues with its existing progress.

**Matching change to note.** The old pattern `research\s+security` had no word boundaries; the new phrase matching requires whole words. Text such as "research securityX" would no longer match. No existing match row depends on this.

**Tests (isolated `CONGRESS_SEARCHES_DIR`; cache reused from the 117th committee-print test)**
- Three-term search (`research security`, `semiconductor*`, `artificial intelligence`) from a terms file: 153 prints, **0 API calls** (all cache hits), 13 matched documents, 20 PDF copies across the three term folders (2 / 7 / 11), 409 match rows (15 / 82 / 312), SQLite counts per term equal; verification confirmed 13/13; validation PASS. "research security" found the same 2 prints and 15 rows as the earlier single-term test.
- Resume with `--name` only: reused saved terms, everything skipped, still 409 rows / 20 PDFs, PASS.
- Refusals with clear messages: same name with different terms; several terms without `--name`; no terms; invalid regex.
- `--term CHIPS --case-sensitive` (default name `chips`): 3 matches, PASS. `--regex --term "research\s+secur\w+"`: 2 matches, website check skipped for the regex term, PASS. `--list` and `validate.py --name` work; `validate.py` without `--name` asks for one when several searches exist.
- Matching unit checks: "AI" doesn't match "said"; `secur*` matches secure/security/securities but not insecure; `*security` matches cybersecurity/insecurity; line breaks and case handled; `C++` and `dual-use` work.

