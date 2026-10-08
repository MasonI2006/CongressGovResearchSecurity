# How to Use the Research Security Crawler

The crawler searches Congress.gov for the phrase "research security", downloads the PDF of every matching document, and records metadata for each match. You choose which Congresses it covers.

## Requirements
- Python 3.10 with the packages in `requirements.txt` (`pip install -r requirements.txt`).
- A Congress.gov API key in `.env` as `CONGRESS_API_KEY=...`.
- Optional but recommended: Poppler's `pdftotext` on PATH, a fallback for PDFs that pypdf can't read.

## Running it
Open a terminal in the project folder (`CongressGovResearchSecurity`, not `src`) and run:

```powershell
python -m src.main --congresses 118
```

| What you want | Command |
|---|---|
| One Congress | `python -m src.main --congresses 118` |
| A range | `python -m src.main --congresses 110-118` |
| A mix | `python -m src.main --congresses 115,117-118` |
| Only some collections | `python -m src.main --congresses 118 --collections bill,hearing` |
| Skip the verification passes | add `--skip-website-search --skip-verification` |

Collection names: `bill`, `amendment`, `committee-report`, `committee-print`, `hearing`, `committee-meeting`, `congressional-record`, `bound-congressional-record`, `crs-report`. Leaving out `--collections` runs all of them.

### Saving a log
For long runs, write the output to a log file:

```powershell
python -u -m src.main --congresses 118 *> run4.log
```

`-u` writes messages to the log as they happen. Without it they are buffered and the log looks frozen. To watch progress from a second terminal:

```powershell
Get-Content run4.log -Wait -Tail 5
```

### Tips
- **Try a small run first**, e.g. `--collections committee-print`, to check that everything works before starting a long run.
- **Expect about 16 hours per Congress.** The API allows about 4,500 calls per hour, and the 119th Congress took about 16 hours.
- **Interrupted runs resume.** After Ctrl+C, a crash or a closed laptop, run the same command again. Finished documents are recorded in `state/` and skipped.
- **Check the archive** at any time with `python validate.py`.

## Which Congresses can I run?
The code accepts any Congress number, but how far back the Congress.gov API has *searchable text* depends on the collection. The table comes from probing a sample of records from the 81st to the 116th Congress, so treat it as approximate.

| Collection | Searchable text starts at |
|---|---|
| Bills | 101st (text only); **103rd** with PDFs |
| Hearings, committee reports | 104th |
| Amendments | 105th |
| Committee prints | 101st (only a handful until the 103rd) |
| Committee meetings | 112th (House); the Senate has some by the 116th (none in the 114th) |
| Daily Congressional Record | 111th (oldest issue August 2009) |
| CRS reports | ~113th–114th (oldest seen: October 2014) |
| Bound Congressional Record | Never: the API exposes only page numbers, no text, so the crawler only checks what exists |

**The 103rd Congress (1993–94) is the practical earliest.** In the 101st and 102nd, bills have text but no PDF, so any match there is recorded as a failure ("no PDF format") instead of being archived.

A full historical run:

```powershell
python -u -m src.main --congresses 103-118 *> run4.log
```

That covers 16 Congresses and will take roughly 10 days, so you may want to run a few Congresses at a time.

## Where the output goes
| Path | Contents |
|---|---|
| `research_security_archive/PDFs/` | One PDF per unique matching document (deduplicated by SHA-256) |
| `research_security_archive/metadata.csv` | One row per PDF |
| `research_security_archive/matches.csv` | One row per phrase occurrence, with surrounding text |
| `research_security_archive/archive.sqlite` | The same data in SQLite, plus people, committees, related items and per-Congress stats |
| `research_security_archive/metadata/` | A JSON sidecar per matched document |
| `research_security_archive/README.txt` | Summary of the latest run, broken down by Congress |
| `raw_api/` | Raw API responses for each matched document |
| `state/dedupe_state.json` | Progress and deduplication state. Keep this file, or runs start over. |
| `cache/` | Cached API responses. Safe to delete, but re-runs will be slower. |

## Running a test without touching the real archive
To send all output to separate folders, set these environment variables before running:

```powershell
$env:RESEARCH_SECURITY_ARCHIVE_DIR = "C:\temp\rs_test\archive"
$env:RESEARCH_SECURITY_STATE_DIR   = "C:\temp\rs_test\state"
$env:CONGRESS_API_CACHE_DIR        = "C:\temp\rs_test\cache"
$env:RESEARCH_SECURITY_RAW_DIR     = "C:\temp\rs_test\raw"
python -m src.main --congresses 117 --collections committee-print
```

They last only for that terminal window. Close it, or `Remove-Item Env:RESEARCH_SECURITY_ARCHIVE_DIR` (and the others), before doing a real run.
