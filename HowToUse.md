# How to Use the Congress.gov Term Search Tool

This tool searches Congress.gov documents for words or phrases you choose. Searchable documents include bills, amendments, committee reports and prints, hearings, committee meeting documents, the Congressional Record and CRS reports. It downloads the PDF of every matching document and records where each term appears, with surrounding text and document metadata. You choose the terms and which Congresses to search.

## Files
Download these into one folder:

| File | What it is |
|---|---|
| `congress_search.py` | The script you run |
| `core.py`, `crawlers.py`, `archive.py` | Code it uses (don't run these directly) |
| `requirements.txt` | Python packages to install |

## Requirements
- Python 3.10 or newer, with the packages in `requirements.txt` (`pip install -r requirements.txt`).
- A free Congress.gov API key (https://api.congress.gov/sign-up/), saved in a file named `.env` in the project folder:
  ```
  CONGRESS_API_KEY=your-key-here
  ```
- Optional but recommended: Poppler's `pdftotext` on PATH, a fallback for PDFs that pypdf can't read.

## Quick start
Open a terminal in that folder and run:

```powershell
python congress_search.py --term "research security" --congresses 118
```

The results go to `searches/research_security/`. For a first try, add `--collections committee-print`, which finishes in minutes.

## Choosing what to search for
| What you want | Command |
|---|---|
| One word or phrase | `--term "machine learning"` |
| Several terms in one run | `--name ai_policy --term "artificial intelligence" --term "machine learning"` |
| A list of terms from a file | `--name ai_policy --terms-file terms.txt` |
| Case-sensitive matching | add `--case-sensitive` (e.g. `--term "CHIPS"` won't match "chips") |
| Regular expressions | add `--regex`, e.g. `--term "research\s+secur\w+"` |

**Several terms are much cheaper than separate runs.** Downloading and reading documents is the slow part, so a run with 10 terms takes about as long as a run with 1.

**A terms file** has one term per line. Blank lines and lines starting with `#` are ignored:
```
# AI policy terms
artificial intelligence
machine learning
neural network*
```

**How matching works**
- Case-insensitive by default. Use `--case-sensitive` to match letter case exactly.
- Whole words only: `AI` matches "AI" but not "said"; `research security` doesn't match "researchsecurity".
- Line and page breaks between words don't matter: "research⏎security" still matches.
- `*` is a wildcard inside a word: `secur*` matches "secure", "security", "securities" (but not "insecure"). `*security` matches "cybersecurity".
- With `--regex`, each term is a Python regular expression, used as written. The text is searched with all whitespace collapsed to single spaces.
- The case and regex options apply to every term in a search.

## Searches are saved
Every search has a **name** and its own folder, `searches/<name>/`. For a single term, the name defaults to the term (`"research security"` becomes `research_security`); a search with several terms needs `--name`. The terms and options are saved in `searches/<name>/search.json`, so later runs only need the name:

```powershell
python congress_search.py --name ai_policy --congresses 117      # add another Congress
python congress_search.py --list                                  # show saved searches and their terms
```

A saved search's terms can't be changed, because its progress records which documents were already checked *for those terms*. To search for something else, start a new name. All searches share one download cache (`cache/`), so a new search over Congresses you've already searched mostly reuses cached responses and runs much faster.

## Choosing where to search
| What you want | Command |
|---|---|
| One Congress | `--congresses 118` |
| A range | `--congresses 110-118` |
| A mix | `--congresses 115,117-118` |
| Only some collections | `--collections bill,hearing` |
| Skip the PDF verification pass | add `--skip-verification` |

Collection names: `bill`, `amendment`, `committee-report`, `committee-print`, `hearing`, `committee-meeting`, `congressional-record`, `bound-congressional-record`, `crs-report`. Leaving out `--collections` searches all of them.

Run `python congress_search.py --help` for the full option list.

### Saving a log
For long runs, write the output to a log file:

```powershell
python -u congress_search.py --name ai_policy --congresses 118 *> ai_policy_118.log
```

`-u` writes messages to the log as they happen. Without it they are buffered and the log looks frozen. To watch progress from a second terminal:

```powershell
Get-Content ai_policy_118.log -Wait -Tail 5
```

### Tips
- **Try a small run first**, e.g. `--collections committee-print`, to check your terms before starting a long run.
- **Expect about 16 hours per Congress** for a first search with an empty cache. The API allows about 4,500 calls per hour. Later searches over the same Congresses are much faster because responses are cached.
- **Interrupted runs resume.** After Ctrl+C, a crash or a closed laptop, run the same command again. Finished documents are recorded in the search's `state/` folder and skipped. Re-running a finished Congress is fast: items whose Congress.gov update date hasn't changed are skipped without new API calls.
- **Check a search's archive** at any time with `python congress_search.py --validate --name <name>`. The name can be left out if there is only one saved search.

## Which Congresses can I search?
The tool accepts any Congress number, but how far back the Congress.gov API has *searchable text* depends on the collection. This table comes from probing a sample of records from the 81st to the 116th Congress, so treat it as approximate.

| Collection | Searchable text starts at |
|---|---|
| Bills | 101st (text only); **103rd** with PDFs |
| Hearings, committee reports | 104th |
| Amendments | 105th |
| Committee prints | 101st (only a handful until the 103rd) |
| Committee meetings | 112th (House); the Senate has some by the 116th (none in the 114th) |
| Daily Congressional Record | 111th (oldest issue August 2009) |
| CRS reports | ~113th–114th (oldest seen: October 2014) |
| Bound Congressional Record | Never: the API exposes only page numbers, no text, so the tool only checks what exists |

**The 103rd Congress (1993–94) is the practical earliest.** In the 101st and 102nd, bills have text but no PDF, so any match there is recorded as a failure ("no PDF format") instead of being archived. Low counts for older Congresses may reflect missing text in the API rather than absence of your terms; check the "Full text searched" column in the search's README.

## Where the output goes
Everything for a search is in `searches/<name>/`:

| Path | Contents |
|---|---|
| `PDFs/` | One-term search: one PDF per unique matching document (deduplicated by SHA-256) |
| `terms/<term>/` | Several-term search: a folder per term with its `PDFs/` and its own `metadata.csv` and `matches.csv`. A document matching two terms is in both folders. |
| `metadata.csv` | One row per PDF, for all terms. `matched_terms` lists the terms it matched. |
| `matches.csv` | One row per occurrence, with the `term`, the exact text matched, the page and surrounding text |
| `archive.sqlite` | The same data in SQLite, plus people (sponsors, cosponsors, witnesses), committees, related items and per-Congress stats |
| `metadata/` | A JSON sidecar per matched document |
| `raw_api/` | Raw API responses for each matched document |
| `README.txt` | Summary of the latest run, by Congress and by term |
| `search.json` | The search's terms and options |
| `state/` | Progress and deduplication state. Keep it, or the search starts over. |

The shared `cache/` folder next to the scripts holds downloaded API responses and documents. It is safe to delete, but searches will be slower afterwards.

The search folder is self-contained (paths inside it are relative), so you can zip it and share it.

## Known limitations
- **Only text the API exposes is searched.** Documents without a text version or PDF in the Congress.gov API can't be searched; older Congresses expose less (see the table above).
- **No OCR.** Scanned, image-only PDFs can contain a term without it being found. After each run, the verification pass lists any saved PDF in which a term couldn't be re-found.
- **CRS reports have no Congress number in the API.** They are assigned to a Congress by publish date, which reflects the latest version, so an older report revised later shows up under the later Congress.
- **No cross-check against the Congress.gov website search.** The website blocks scripted access, so results come only from the API.

## Running a test without touching your real searches
Set these environment variables to send all output and cache to separate folders:

```powershell
$env:CONGRESS_SEARCHES_DIR  = "C:\temp\test\searches"
$env:CONGRESS_API_CACHE_DIR = "C:\temp\test\cache"
python congress_search.py --term "semiconductor*" --congresses 117 --collections committee-print
```

They last only for that terminal window. Close it, or run `Remove-Item Env:CONGRESS_SEARCHES_DIR, Env:CONGRESS_API_CACHE_DIR`, before a real run.
