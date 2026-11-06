"""Collection crawlers and the document pipeline. Each crawler pages through a
Congress.gov collection for one Congress and hands every document with text to
process_document, which searches it for the terms and archives the PDF of any match."""
from datetime import date

from tqdm import tqdm

import archive
import core


# ============================================================================
# Collection crawlers
# ============================================================================

BILL_TYPE_SLUG = {
    "hr": "house-bill", "s": "senate-bill", "hjres": "house-joint-resolution",
    "sjres": "senate-joint-resolution", "hconres": "house-concurrent-resolution",
    "sconres": "senate-concurrent-resolution", "hres": "house-resolution", "sres": "senate-resolution",
}
AMDT_TYPE_SLUG = {"hamdt": "house-amendment", "samdt": "senate-amendment", "suamdt": "senate-amendment"}


def _fmt_url(fmt):
    return fmt.get("url") or fmt.get("URL")


def _normalize_formats(raw_formats):
    out = []
    for fmt in raw_formats or []:
        u = _fmt_url(fmt)
        t = fmt.get("type") or fmt.get("Type") or fmt.get("format") or fmt.get("Format")
        if u and t:
            out.append({"url": u, "type": t})
    return out


def _count(path, params=None):
    data = core.api_get(path, params or {"limit": 1})
    return data.get("pagination", {}).get("count", 0)


def crawl_bills(congress):
    total = _count(f"bill/{congress}")
    matched = 0
    for item in tqdm(core.api_paginate(f"bill/{congress}"), total=total, desc=f"bills/{congress}", unit="bill"):
        btype = item["type"].lower()
        number = item["number"]
        parent_key = f"bill/{congress}/{btype}/{number}"
        marker = item.get("updateDateIncludingText") or item.get("updateDate")
        if core.parent_done(parent_key, marker):
            continue
        try:
            text_data = core.api_get(f"bill/{congress}/{btype}/{number}/text")
        except Exception:
            continue
        versions = text_data.get("textVersions", [])
        congress_url = f"https://www.congress.gov/bill/{core.congress_ordinal(congress)}-congress/{BILL_TYPE_SLUG.get(btype, btype)}/{number}"
        doc_ids = []
        for tv in versions:
            vtype = (tv.get("type") or "unknown").replace(" ", "_").replace("/", "-")
            formats = _normalize_formats(tv.get("formats"))
            if not formats:
                continue
            doc_id = f"BILL-{congress}-{item['type']}-{number}-{vtype}"
            doc_ids.append(doc_id)
            ok = process_document(
                doc_id=doc_id, collection="bill", congress=congress,
                document_type=item["type"], document_number=number,
                title=item.get("title"), date=tv.get("date"), congress_url=congress_url,
                formats=formats, api_endpoint=f"/bill/{congress}/{btype}/{number}/text",
            )
            matched += int(ok)
        core.mark_parent_done(parent_key, marker, doc_ids)
    return matched


def crawl_amendments(congress):
    total = 0
    for amdt_type in ("hamdt", "samdt", "suamdt"):
        try:
            total += _count(f"amendment/{congress}/{amdt_type}")
        except Exception:
            pass
    matched = 0
    for amdt_type in ("hamdt", "samdt", "suamdt"):
        try:
            sub_total = _count(f"amendment/{congress}/{amdt_type}")
        except Exception:
            continue
        for item in tqdm(core.api_paginate(f"amendment/{congress}/{amdt_type}"), total=sub_total,
                          desc=f"amendments/{congress}/{amdt_type}", unit="amdt"):
            number = item["number"]
            parent_key = f"amendment/{congress}/{amdt_type}/{number}"
            marker = item.get("updateDate")
            if core.parent_done(parent_key, marker):
                continue
            try:
                text_data = core.api_get(f"amendment/{congress}/{amdt_type}/{number}/text")
            except Exception:
                continue
            versions = text_data.get("textVersions", [])
            congress_url = f"https://www.congress.gov/amendment/{core.congress_ordinal(congress)}-congress/{AMDT_TYPE_SLUG.get(amdt_type, amdt_type)}/{number}"
            doc_ids = []
            for tv in versions:
                vtype = (tv.get("type") or "unknown").replace(" ", "_").replace("/", "-")
                formats = _normalize_formats(tv.get("formats"))
                if not formats:
                    continue
                doc_id = f"AMDT-{congress}-{amdt_type.upper()}-{number}-{vtype}"
                doc_ids.append(doc_id)
                ok = process_document(
                    doc_id=doc_id, collection="amendment", congress=congress,
                    document_type=amdt_type.upper(), document_number=number,
                    title=item.get("purpose") or item.get("description"), date=tv.get("date"),
                    congress_url=congress_url, formats=formats,
                    api_endpoint=f"/amendment/{congress}/{amdt_type}/{number}/text",
                )
                matched += int(ok)
            core.mark_parent_done(parent_key, marker, doc_ids)
    return matched


REPORT_TYPES = ["hrpt", "srpt", "erpt"]


def crawl_committee_reports(congress):
    matched = 0
    for rtype in REPORT_TYPES:
        try:
            total = _count(f"committee-report/{congress}/{rtype}")
        except Exception:
            continue
        for item in tqdm(core.api_paginate(f"committee-report/{congress}/{rtype}"), total=total,
                          desc=f"committee-report/{congress}/{rtype}", unit="rpt"):
            number = item["number"]
            part = item.get("part", 1)
            parent_key = f"committee-report/{congress}/{rtype}/{number}/{part}"
            marker = item.get("updateDate")
            if core.parent_done(parent_key, marker):
                continue
            try:
                detail = core.api_get(f"committee-report/{congress}/{rtype}/{number}")
            except Exception:
                detail = {}
            detail_items = detail.get("committeeReports") or detail.get("committeeReport") or []
            detail_item = detail_items[0] if isinstance(detail_items, list) and detail_items else (detail_items if isinstance(detail_items, dict) else {})
            try:
                text_data = core.api_get(f"committee-report/{congress}/{rtype}/{number}/text")
            except Exception:
                continue
            # The committee-report text endpoint returns a list of entries that
            # may each wrap a single file format (e.g. one entry for Formatted Text,
            # a separate entry for PDF) rather than grouping them together, so
            # merge every format across all entries into one list per report.
            entries = text_data.get("textVersions") or text_data.get("text") or []
            formats = []
            for tv in entries:
                if _fmt_url(tv):
                    formats.extend(_normalize_formats([tv]))
                else:
                    formats.extend(_normalize_formats(tv.get("formats")))
            if not formats:
                core.mark_parent_done(parent_key, marker, [])
                continue
            congress_url = f"https://www.congress.gov/congressional-report/{core.congress_ordinal(congress)}-congress/{'house-report' if rtype == 'hrpt' else ('senate-report' if rtype == 'srpt' else 'executive-report')}/{number}"
            doc_id = f"RPT-{congress}-{rtype.upper()}-{number}-{part}"
            ok = process_document(
                doc_id=doc_id, collection="committee-report", congress=congress,
                document_type=rtype.upper(), document_number=f"{number}-{part}",
                title=detail_item.get("title") or item.get("title"),
                date=detail_item.get("issueDate") or item.get("updateDate"),
                congress_url=congress_url, formats=formats,
                api_endpoint=f"/committee-report/{congress}/{rtype}/{number}/text",
            )
            matched += int(ok)
            core.mark_parent_done(parent_key, marker, [doc_id])
    return matched


def crawl_committee_prints(congress):
    matched = 0
    for chamber in ("house", "senate"):
        try:
            total = _count(f"committee-print/{congress}/{chamber}")
        except Exception:
            continue
        for item in tqdm(core.api_paginate(f"committee-print/{congress}/{chamber}"), total=total,
                          desc=f"committee-print/{congress}/{chamber}", unit="prt"):
            jacket = item["jacketNumber"]
            parent_key = f"committee-print/{congress}/{chamber}/{jacket}"
            marker = item.get("updateDate")
            if core.parent_done(parent_key, marker):
                continue
            try:
                detail = core.api_get(f"committee-print/{congress}/{chamber}/{jacket}")
            except Exception:
                detail = {}
            detail_items = detail.get("committeePrint") or []
            detail_item = detail_items[0] if isinstance(detail_items, list) and detail_items else (detail_items if isinstance(detail_items, dict) else {})
            try:
                text_data = core.api_get(f"committee-print/{congress}/{chamber}/{jacket}/text")
            except Exception:
                continue
            # Entries may be flat format items themselves, or wrap a "formats"
            # sublist; merge everything into one list per print.
            entries = text_data.get("textVersions") or text_data.get("text") or []
            formats = []
            for tv in entries:
                if _fmt_url(tv):
                    formats.extend(_normalize_formats([tv]))
                else:
                    formats.extend(_normalize_formats(tv.get("formats")))
            if not formats:
                core.mark_parent_done(parent_key, marker, [])
                continue
            congress_url = item.get("url", "")
            doc_id = f"CPRT-{congress}-{chamber.upper()}-{jacket}"
            ok = process_document(
                doc_id=doc_id, collection="committee-print", congress=congress,
                document_type="CPRT", document_number=jacket,
                title=detail_item.get("title") or item.get("title"),
                date=detail_item.get("updateDate") or item.get("updateDate"),
                congress_url=congress_url, formats=formats,
                api_endpoint=f"/committee-print/{congress}/{chamber}/{jacket}/text",
            )
            matched += int(ok)
            core.mark_parent_done(parent_key, marker, [doc_id])
    return matched


def crawl_hearings(congress):
    matched = 0
    for chamber in ("house", "senate", "nochamber"):
        try:
            total = _count(f"hearing/{congress}/{chamber}")
        except Exception:
            continue
        for item in tqdm(core.api_paginate(f"hearing/{congress}/{chamber}"), total=total,
                          desc=f"hearing/{congress}/{chamber}", unit="hrg"):
            jacket = item["jacketNumber"]
            parent_key = f"hearing/{congress}/{chamber}/{jacket}"
            marker = item.get("updateDate")
            if core.parent_done(parent_key, marker):
                continue
            try:
                detail = core.api_get(f"hearing/{congress}/{chamber}/{jacket}")
            except Exception:
                continue
            hearing = detail.get("hearing", detail)
            formats = _normalize_formats(hearing.get("formats"))
            if not formats:
                core.mark_parent_done(parent_key, marker, [])
                continue
            dates = hearing.get("dates") or []
            hdate = dates[0]["date"] if dates else None
            doc_id = f"HRG-{congress}-{chamber.upper()}-{jacket}"
            ok = process_document(
                doc_id=doc_id, collection="hearing", congress=congress,
                document_type="HRG", document_number=jacket,
                title=hearing.get("title"), date=hdate,
                congress_url=item.get("url", ""), formats=formats,
                api_endpoint=f"/hearing/{congress}/{chamber}/{jacket}",
            )
            matched += int(ok)
            core.mark_parent_done(parent_key, marker, [doc_id])
    return matched


def crawl_committee_meetings(congress):
    matched = 0
    for chamber in ("house", "senate", "nochamber"):
        try:
            total = _count(f"committee-meeting/{congress}/{chamber}")
        except Exception:
            continue
        for item in tqdm(core.api_paginate(f"committee-meeting/{congress}/{chamber}"), total=total,
                          desc=f"committee-meeting/{congress}/{chamber}", unit="mtg"):
            event_id = item["eventId"]
            parent_key = f"committee-meeting/{congress}/{chamber}/{event_id}"
            marker = item.get("updateDate")
            if core.parent_done(parent_key, marker):
                continue
            try:
                detail = core.api_get(f"committee-meeting/{congress}/{chamber}/{event_id}")
            except Exception:
                continue
            meeting = detail.get("committeeMeeting", detail)
            title = meeting.get("title")
            mdate = meeting.get("date")
            congress_url = item.get("url", "")
            docs = list(meeting.get("meetingDocuments") or [])
            for wd in meeting.get("witnessDocuments") or []:
                wd = dict(wd)
                wd.setdefault("documentType", "Witness Document")
                docs.append(wd)
            doc_ids = []
            for idx, d in enumerate(docs):
                url = d.get("url")
                if not url:
                    continue
                fmt_type = d.get("format") or "PDF"
                formats = [{"url": url, "type": fmt_type if fmt_type in ("PDF",) else "PDF"}]
                dtype = d.get("documentType", "Document")
                doc_id = f"MTG-{congress}-{chamber.upper()}-{event_id}-{idx}-{dtype}".replace(" ", "_")
                doc_ids.append(doc_id)
                ok = process_document(
                    doc_id=doc_id, collection="committee-meeting", congress=congress,
                    document_type=dtype, document_number=event_id,
                    title=f"{title} :: {d.get('name', dtype)}", date=mdate,
                    congress_url=congress_url, formats=formats,
                    api_endpoint=f"/committee-meeting/{congress}/{chamber}/{event_id}",
                )
                matched += int(ok)
            core.mark_parent_done(parent_key, marker, doc_ids)
    return matched


def crawl_daily_congressional_record(congress):
    matched = 0
    total = _count("daily-congressional-record")
    for item in tqdm(core.api_paginate("daily-congressional-record"), total=total,
                      desc=f"daily-congressional-record (filter congress={congress})", unit="issue"):
        issue_congress = item.get("congress")
        try:
            issue_congress = int(issue_congress)
        except (TypeError, ValueError):
            issue_congress = None
        if issue_congress != congress:
            continue
        volume = item.get("volumeNumber")
        issue = item.get("issueNumber")
        if volume is None or issue is None:
            continue
        parent_key = f"daily-congressional-record/{volume}/{issue}"
        marker = item.get("updateDate")
        if core.parent_done(parent_key, marker):
            continue
        try:
            data = core.api_get(f"daily-congressional-record/{volume}/{issue}/articles")
        except Exception:
            continue
        sections = data.get("articles", [])
        doc_ids = []
        for sec_idx, section in enumerate(sections):
            sec_name = section.get("name", f"section{sec_idx}")
            for art_idx, art in enumerate(section.get("sectionArticles", [])):
                formats = _normalize_formats(art.get("text"))
                if not formats:
                    continue
                doc_id = f"CREC-{congress}-V{volume}-I{issue}-{sec_name}-{art_idx}".replace(" ", "_")
                doc_ids.append(doc_id)
                ok = process_document(
                    doc_id=doc_id, collection="congressional-record", congress=congress,
                    document_type="CREC-daily", document_number=f"V{volume}I{issue}",
                    title=art.get("title"), date=item.get("issueDate"),
                    congress_url=item.get("url", ""), formats=formats,
                    api_endpoint=f"/daily-congressional-record/{volume}/{issue}/articles",
                )
                matched += int(ok)
        core.mark_parent_done(parent_key, marker, doc_ids)
    return matched


BOUND_RECORD_TEXT_KEYS = ("text", "formats", "fullIssue", "dailyDigest")


def crawl_bound_congressional_record(congress):
    """Probe the Bound Congressional Record for the Congress's session years.

    As of 2026-10 the API exposes only page-range metadata for bound volumes
    (congress, date, sections[name/startPage/endPage]) with no text or PDF
    links, so there is nothing to phrase-search. We still probe each year so
    the absence is verified rather than assumed, and warn if a text-bearing
    field ever appears so the crawler can be extended.
    """
    core.counter("bound-congressional-record", congress)
    for year in core.congress_years(congress):
        try:
            # Some years (e.g. 2021-2022 as of 2026-10) return a persistent HTTP 500,
            # so don't spend the full retry/backoff budget on this metadata-only probe.
            listing = core.api_get(f"bound-congressional-record/{year}", {"limit": 1}, max_attempts=2)
        except Exception as e:
            print(f"bound-congressional-record/{year}: API error, not probed ({core.redact_text(str(e))})")
            continue
        count = listing.get("pagination", {}).get("count", 0)
        items = listing.get("boundCongressionalRecord") or []
        if not items:
            print(f"bound-congressional-record/{year}: no bound entries in the API")
            continue
        text_keys = set(BOUND_RECORD_TEXT_KEYS) & set(items[0])
        entry_date = items[0].get("date") or ""
        if entry_date.count("-") == 2:
            y, m, d = (int(part) for part in entry_date[:10].split("-"))
            try:
                detail = core.api_get(f"bound-congressional-record/{y}/{m}/{d}", {"limit": 1},
                                        max_attempts=2)
                for entry in detail.get("boundCongressionalRecord") or []:
                    text_keys |= set(BOUND_RECORD_TEXT_KEYS) & set(entry)
            except Exception:
                pass
        if text_keys:
            print(f"WARN: bound-congressional-record/{year} now exposes {sorted(text_keys)}; "
                  "the crawler only probes and must be extended to search it")
        else:
            print(f"bound-congressional-record/{year}: {count} entries, page-range metadata only "
                  "(no text/PDF exposed by the API); not searchable")
    return 0


def crawl_crs_reports(congress):
    """CRS reports aren't tagged by congress; filter by publish date falling
    within the target congress's date range."""
    start, end = core.congress_date_range(congress)
    total = _count("crsreport")
    matched = 0
    for item in tqdm(core.api_paginate("crsreport"), total=total, desc=f"crsreport (filter congress={congress})", unit="rpt"):
        pub = item.get("publishDate")
        if not pub:
            continue
        try:
            pub_date = date.fromisoformat(pub[:10])
        except ValueError:
            continue
        if not (start <= pub_date < end):
            continue
        rid = item.get("id")
        parent_key = f"crsreport/{rid}"
        marker = item.get("updateDate")
        if core.parent_done(parent_key, marker):
            continue
        try:
            detail = core.api_get(f"crsreport/{rid}")
        except Exception:
            continue
        rep = detail.get("CRSReport", detail)
        formats = _normalize_formats(rep.get("formats"))
        if not formats:
            core.mark_parent_done(parent_key, marker, [])
            continue
        # The detail payload calls the version "currentVersion"; list entries call it "version".
        version = rep.get("currentVersion") or rep.get("version") or item.get("version") or 1
        doc_id = f"CRS-{rid}-v{version}"
        ok = process_document(
            doc_id=doc_id, collection="crs-report", congress=congress,
            document_type="CRS", document_number=rid,
            title=rep.get("title"), date=pub,
            congress_url=rep.get("url", ""), formats=formats,
            api_endpoint=f"/crsreport/{rid}",
        )
        matched += int(ok)
        core.mark_parent_done(parent_key, marker, [doc_id])
    return matched


# Collection name (as used on the command line) -> crawler, in default run order.
CRAWLERS = {
    "bill": crawl_bills,
    "amendment": crawl_amendments,
    "committee-report": crawl_committee_reports,
    "committee-print": crawl_committee_prints,
    "hearing": crawl_hearings,
    "committee-meeting": crawl_committee_meetings,
    "congressional-record": crawl_daily_congressional_record,
    "bound-congressional-record": crawl_bound_congressional_record,
    "crs-report": crawl_crs_reports,
}


# ============================================================================
# Run-time estimate
# ============================================================================
# Seconds per listed item on a first run with an empty cache, measured on the
# full 119th-Congress run (October 2026, about 16 hours). That run downloaded
# PDFs this version skips, so real runs are often somewhat faster. Items that
# are already in the download cache cost roughly a tenth as much; items this
# search already finished cost nothing.
WARM_FRACTION = 0.1
CR_SECONDS_PER_CONGRESS = 8000      # daily Congressional Record, a full Congress (111th+)
CR_ISSUES_PER_CONGRESS = 690
CRS_SECONDS_PER_CONGRESS = 2500     # CRS reports published during a recent Congress
LISTING_SCAN_SECONDS = 60           # paging through a full listing (CR, CRS)


def _listings(collection, c):
    """(list path, seconds per item, finished-item key prefix, item -> path of its
    first per-item API call) for every list the crawler pages through."""
    if collection == "bill":
        return [(f"bill/{c}", 1.1, f"bill/{c}/",
                 lambda it: f"bill/{c}/{it['type'].lower()}/{it['number']}/text")]
    if collection == "amendment":
        return [(f"amendment/{c}/{t}", 1.0, f"amendment/{c}/{t}/",
                 lambda it, t=t: f"amendment/{c}/{t}/{it['number']}/text") for t in ("hamdt", "samdt", "suamdt")]
    if collection == "committee-report":
        return [(f"committee-report/{c}/{t}", 1.8, f"committee-report/{c}/{t}/",
                 lambda it, t=t: f"committee-report/{c}/{t}/{it['number']}") for t in REPORT_TYPES]
    if collection == "committee-print":
        return [(f"committee-print/{c}/{ch}", 1.8, f"committee-print/{c}/{ch}/",
                 lambda it, ch=ch: f"committee-print/{c}/{ch}/{it['jacketNumber']}") for ch in ("house", "senate")]
    if collection == "hearing":
        return [(f"hearing/{c}/{ch}", 3.0, f"hearing/{c}/{ch}/",
                 lambda it, ch=ch: f"hearing/{c}/{ch}/{it['jacketNumber']}")
                for ch in ("house", "senate", "nochamber")]
    if collection == "committee-meeting":
        # House meeting documents are mostly PDFs that must be downloaded and read.
        rates = {"house": 8.2, "senate": 0.8, "nochamber": 0.8}
        return [(f"committee-meeting/{c}/{ch}", rates[ch], f"committee-meeting/{c}/{ch}/",
                 lambda it, ch=ch: f"committee-meeting/{c}/{ch}/{it['eventId']}") for ch in rates]
    return []


def estimate(collection, congress):
    """Rough (seconds, items left) for crawling one collection in one Congress,
    allowing for this search's finished items and the shared download cache.
    Makes about one API call per list (answers are cached and reused by the crawl)."""
    if collection == "congressional-record":
        if congress < 111:          # the API's daily Record starts in August 2009
            return LISTING_SCAN_SECONDS, 0
        share = 0.7 if congress == 111 else 1.0
        issues = int(CR_ISSUES_PER_CONGRESS * share)
        volumes = {year - 1854 for year in core.congress_years(congress)}
        done = sum(core.count_parents_done(f"daily-congressional-record/{v}/") for v in volumes)
        left = max(0.0, 1 - done / issues)
        return LISTING_SCAN_SECONDS + CR_SECONDS_PER_CONGRESS * share * left, int(issues * left)
    if collection == "crs-report":     # oldest CRS products in the API date from late 2014
        share = 1.0 if congress >= 114 else (0.5 if congress == 113 else 0.0)
        return LISTING_SCAN_SECONDS + CRS_SECONDS_PER_CONGRESS * share, int(2600 * share)
    if collection == "bound-congressional-record":
        return 10, 0

    seconds, items = 0.0, 0
    for path, rate, prefix, item_path in _listings(collection, congress):
        crawled_before = core.is_cached(path, {"limit": 1})
        try:
            count = _count(path)
        except Exception:
            continue
        left = max(0, count - core.count_parents_done(prefix))
        cached_share = 0.0
        if crawled_before and left:
            # Sample the first page (the crawl's own first request) for cache hits.
            try:
                sample = core.list_items(core.api_get(path, {"limit": core.PAGE_SIZE, "offset": 0}))
                if sample:
                    cached_share = sum(core.is_cached(item_path(it)) for it in sample) / len(sample)
            except Exception:
                pass
        seconds += left * rate * (1 - cached_share * (1 - WARM_FRACTION))
        items += left
    return seconds, items


# ============================================================================
# Document pipeline
# ============================================================================

NON_PDF_PRIORITY = ["Formatted Text", "Generated HTML", "HTML", "Formatted XML", "XML"]


def _pick_format(formats, wanted_types):
    for wanted in wanted_types:
        for fmt in formats:
            if fmt.get("type") == wanted:
                return fmt
    return None


def _pick_pdf(formats):
    for fmt in formats:
        if fmt.get("type") == "PDF":
            return fmt
    return None


def _looks_like_markup(text: str) -> bool:
    stripped = text.lstrip()
    return stripped.startswith("<")


def process_document(doc_id, collection, congress, document_type, document_number,
                      title, date, congress_url, formats, api_endpoint):
    """Search a document's available text for the search terms; if any is found,
    download the PDF, dedupe, and record metadata/match rows. Returns True if a match was found.
    Never raises: any unexpected failure is logged and treated as no-match, so a
    single bad document can't abort a multi-hour crawl.
    """
    try:
        return _process_document_inner(
            doc_id, collection, congress, document_type, document_number,
            title, date, congress_url, formats, api_endpoint,
        )
    except Exception as e:
        error = core.redact_text(str(e))
        core.record_processing_failure(doc_id, collection, error)
        print(f"WARN: process_document failed for {error} [document={doc_id}]")
        return False


def _process_document_inner(doc_id, collection, congress, document_type, document_number,
                             title, date, congress_url, formats, api_endpoint):
    if core.already_processed(doc_id):
        return False

    formats = formats or []
    non_pdf_fmt = _pick_format(formats, NON_PDF_PRIORITY)
    pdf_fmt = _pick_pdf(formats)

    matches = []  # list of (page_number, context, char_location, term, surface)
    full_text_searched = False

    if non_pdf_fmt:
        raw = core.fetch_text(non_pdf_fmt["url"])
        if raw is not None:
            full_text_searched = True
            text = core.html_to_text(raw) if _looks_like_markup(raw) else raw
            for m in core.find_matches(text):
                matches.append((None, m["context"], m["start"], m["term"], m["surface"]))

    # The PDF is only needed when there is no text version (or it failed to
    # download), or later for a match.
    pdf_bytes = None
    if pdf_fmt and not full_text_searched:
        pdf_bytes = core.fetch_binary(pdf_fmt["url"])
        if pdf_bytes:
            full_text_searched = True
            pages = core.extract_pdf_pages_text(pdf_bytes)
            for page_num, page_text in pages:
                for m in core.find_matches(page_text):
                    matches.append((page_num, m["context"], m["start"], m["term"], m["surface"]))
            # Cross-page-boundary matches: join consecutive page texts.
            for i in range(len(pages) - 1):
                joined = (pages[i][1][-250:] + " " + pages[i + 1][1][:250])
                for m in core.find_matches(joined):
                    ctx = m["context"]
                    if not any(ctx[:40] in existing[1] and existing[3] == m["term"] for existing in matches):
                        matches.append((f"{pages[i][0]}-{pages[i+1][0]}", ctx, None, m["term"], m["surface"]))

    def note_failed_download(problem, url):
        core.record_failed_download(doc_id, collection, congress, title, problem, url, congress_url)

    if not matches:
        if not full_text_searched and (non_pdf_fmt or pdf_fmt):
            note_failed_download("could not download it, so it was not searched",
                                 (pdf_fmt or non_pdf_fmt)["url"])
        core.note_examined(collection, congress, full_text_searched, doc_id)
        core.mark_processed(doc_id)
        return False

    core.mark_match_pending(doc_id, collection)
    if not pdf_fmt:
        note_failed_download("matched, but the API has no PDF for it (link is the text version)",
                             non_pdf_fmt["url"] if non_pdf_fmt else "")
        raise RuntimeError(f"Matched document has no PDF format: {doc_id}")
    if pdf_bytes is None:
        pdf_bytes = core.fetch_binary(pdf_fmt["url"])
    if not pdf_bytes:
        note_failed_download("matched, but the PDF download failed", pdf_fmt["url"])
        raise RuntimeError(f"Matched document PDF could not be downloaded: {doc_id}")

    sha = core.sha256_bytes(pdf_bytes)
    candidate_name = core.filename_for_hash(sha, core.safe_filename(doc_id, pdf_fmt["url"]))
    matched_terms = [term for term in core.SEARCH.terms if any(m[3] == term for m in matches)]
    # Each matching term's folder gets its own copy (one shared folder for a one-term search).
    for term in matched_terms:
        out_path = core.term_pdf_dir(term) / candidate_name
        if out_path.exists():
            if core.sha256_bytes(out_path.read_bytes()) != sha:
                raise RuntimeError(f"Existing PDF hash differs from downloaded content: {out_path}")
        else:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(pdf_bytes)

    core.note_examined(collection, congress, full_text_searched, doc_id)
    archive.capture_match(
        collection=collection, document_id=doc_id, congress=congress,
        document_type=document_type, document_number=document_number, title=title,
        document_date=date, congress_url=congress_url, api_endpoint=api_endpoint,
        pdf_url=pdf_fmt["url"], pdf_bytes=pdf_bytes, formats=formats,
        used_text_type=non_pdf_fmt.get("type") if non_pdf_fmt else None,
        matches=matches, filename=candidate_name, matched_terms=matched_terms,
    )
    core.remember_pdf(sha, candidate_name, collection, congress, len(matches))
    core.mark_processed(doc_id)
    core.clear_failed_download(doc_id)

    return True
