"""Per-collection crawlers. Each function iterates a full collection (all pages,
all items) for a given congress and feeds candidate documents into
processor.process_document for phrase searching + PDF capture.
"""
from datetime import date

from tqdm import tqdm

from . import client, config, processor

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
    data = client.api_get(path, params or {"limit": 1})
    return data.get("pagination", {}).get("count", 0)


def crawl_bills(congress):
    total = _count(f"bill/{congress}")
    matched = 0
    for item in tqdm(client.api_paginate(f"bill/{congress}"), total=total, desc=f"bills/{congress}", unit="bill"):
        btype = item["type"].lower()
        number = item["number"]
        try:
            text_data = client.api_get(f"bill/{congress}/{btype}/{number}/text")
        except Exception:
            continue
        versions = text_data.get("textVersions", [])
        congress_url = f"https://www.congress.gov/bill/{config.congress_ordinal(congress)}-congress/{BILL_TYPE_SLUG.get(btype, btype)}/{number}"
        for tv in versions:
            vtype = (tv.get("type") or "unknown").replace(" ", "_").replace("/", "-")
            formats = _normalize_formats(tv.get("formats"))
            if not formats:
                continue
            doc_id = f"BILL-{congress}-{item['type']}-{number}-{vtype}"
            ok = processor.process_document(
                doc_id=doc_id, collection="bill", congress=congress,
                document_type=item["type"], document_number=number,
                title=item.get("title"), date=tv.get("date"), congress_url=congress_url,
                formats=formats, api_endpoint=f"/bill/{congress}/{btype}/{number}/text",
            )
            matched += int(ok)
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
        for item in tqdm(client.api_paginate(f"amendment/{congress}/{amdt_type}"), total=sub_total,
                          desc=f"amendments/{congress}/{amdt_type}", unit="amdt"):
            number = item["number"]
            try:
                text_data = client.api_get(f"amendment/{congress}/{amdt_type}/{number}/text")
            except Exception:
                continue
            versions = text_data.get("textVersions", [])
            congress_url = f"https://www.congress.gov/amendment/{config.congress_ordinal(congress)}-congress/{AMDT_TYPE_SLUG.get(amdt_type, amdt_type)}/{number}"
            for tv in versions:
                vtype = (tv.get("type") or "unknown").replace(" ", "_").replace("/", "-")
                formats = _normalize_formats(tv.get("formats"))
                if not formats:
                    continue
                doc_id = f"AMDT-{congress}-{amdt_type.upper()}-{number}-{vtype}"
                ok = processor.process_document(
                    doc_id=doc_id, collection="amendment", congress=congress,
                    document_type=amdt_type.upper(), document_number=number,
                    title=item.get("purpose") or item.get("description"), date=tv.get("date"),
                    congress_url=congress_url, formats=formats,
                    api_endpoint=f"/amendment/{congress}/{amdt_type}/{number}/text",
                )
                matched += int(ok)
    return matched


REPORT_TYPES = ["hrpt", "srpt", "erpt"]


def crawl_committee_reports(congress):
    matched = 0
    for rtype in REPORT_TYPES:
        try:
            total = _count(f"committee-report/{congress}/{rtype}")
        except Exception:
            continue
        for item in tqdm(client.api_paginate(f"committee-report/{congress}/{rtype}"), total=total,
                          desc=f"committee-report/{congress}/{rtype}", unit="rpt"):
            number = item["number"]
            part = item.get("part", 1)
            try:
                detail = client.api_get(f"committee-report/{congress}/{rtype}/{number}")
            except Exception:
                detail = {}
            detail_items = detail.get("committeeReports") or detail.get("committeeReport") or []
            detail_item = detail_items[0] if isinstance(detail_items, list) and detail_items else (detail_items if isinstance(detail_items, dict) else {})
            try:
                text_data = client.api_get(f"committee-report/{congress}/{rtype}/{number}/text")
            except Exception:
                continue
            # The committee-report text endpoint returns a list of entries that
            # may each wrap a single format (e.g. one entry for Formatted Text,
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
                continue
            congress_url = f"https://www.congress.gov/congressional-report/{config.congress_ordinal(congress)}-congress/{'house-report' if rtype == 'hrpt' else ('senate-report' if rtype == 'srpt' else 'executive-report')}/{number}"
            doc_id = f"RPT-{congress}-{rtype.upper()}-{number}-{part}"
            ok = processor.process_document(
                doc_id=doc_id, collection="committee-report", congress=congress,
                document_type=rtype.upper(), document_number=f"{number}-{part}",
                title=detail_item.get("title") or item.get("title"),
                date=detail_item.get("issueDate") or item.get("updateDate"),
                congress_url=congress_url, formats=formats,
                api_endpoint=f"/committee-report/{congress}/{rtype}/{number}/text",
            )
            matched += int(ok)
    return matched


def crawl_committee_prints(congress):
    matched = 0
    for chamber in ("house", "senate"):
        try:
            total = _count(f"committee-print/{congress}/{chamber}")
        except Exception:
            continue
        for item in tqdm(client.api_paginate(f"committee-print/{congress}/{chamber}"), total=total,
                          desc=f"committee-print/{congress}/{chamber}", unit="prt"):
            jacket = item["jacketNumber"]
            try:
                detail = client.api_get(f"committee-print/{congress}/{chamber}/{jacket}")
            except Exception:
                detail = {}
            detail_items = detail.get("committeePrint") or []
            detail_item = detail_items[0] if isinstance(detail_items, list) and detail_items else (detail_items if isinstance(detail_items, dict) else {})
            try:
                text_data = client.api_get(f"committee-print/{congress}/{chamber}/{jacket}/text")
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
                continue
            congress_url = item.get("url", "")
            doc_id = f"CPRT-{congress}-{chamber.upper()}-{jacket}"
            ok = processor.process_document(
                doc_id=doc_id, collection="committee-print", congress=congress,
                document_type="CPRT", document_number=jacket,
                title=detail_item.get("title") or item.get("title"),
                date=detail_item.get("updateDate") or item.get("updateDate"),
                congress_url=congress_url, formats=formats,
                api_endpoint=f"/committee-print/{congress}/{chamber}/{jacket}/text",
            )
            matched += int(ok)
    return matched


def crawl_hearings(congress):
    matched = 0
    for chamber in ("house", "senate", "nochamber"):
        try:
            total = _count(f"hearing/{congress}/{chamber}")
        except Exception:
            continue
        for item in tqdm(client.api_paginate(f"hearing/{congress}/{chamber}"), total=total,
                          desc=f"hearing/{congress}/{chamber}", unit="hrg"):
            jacket = item["jacketNumber"]
            try:
                detail = client.api_get(f"hearing/{congress}/{chamber}/{jacket}")
            except Exception:
                continue
            hearing = detail.get("hearing", detail)
            formats = _normalize_formats(hearing.get("formats"))
            if not formats:
                continue
            dates = hearing.get("dates") or []
            hdate = dates[0]["date"] if dates else None
            doc_id = f"HRG-{congress}-{chamber.upper()}-{jacket}"
            ok = processor.process_document(
                doc_id=doc_id, collection="hearing", congress=congress,
                document_type="HRG", document_number=jacket,
                title=hearing.get("title"), date=hdate,
                congress_url=item.get("url", ""), formats=formats,
                api_endpoint=f"/hearing/{congress}/{chamber}/{jacket}",
            )
            matched += int(ok)
    return matched


def crawl_committee_meetings(congress):
    matched = 0
    for chamber in ("house", "senate", "nochamber"):
        try:
            total = _count(f"committee-meeting/{congress}/{chamber}")
        except Exception:
            continue
        for item in tqdm(client.api_paginate(f"committee-meeting/{congress}/{chamber}"), total=total,
                          desc=f"committee-meeting/{congress}/{chamber}", unit="mtg"):
            event_id = item["eventId"]
            try:
                detail = client.api_get(f"committee-meeting/{congress}/{chamber}/{event_id}")
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
            for idx, d in enumerate(docs):
                url = d.get("url")
                if not url:
                    continue
                fmt_type = d.get("format") or "PDF"
                formats = [{"url": url, "type": fmt_type if fmt_type in ("PDF",) else "PDF"}]
                dtype = d.get("documentType", "Document")
                doc_id = f"MTG-{congress}-{chamber.upper()}-{event_id}-{idx}-{dtype}".replace(" ", "_")
                ok = processor.process_document(
                    doc_id=doc_id, collection="committee-meeting", congress=congress,
                    document_type=dtype, document_number=event_id,
                    title=f"{title} :: {d.get('name', dtype)}", date=mdate,
                    congress_url=congress_url, formats=formats,
                    api_endpoint=f"/committee-meeting/{congress}/{chamber}/{event_id}",
                )
                matched += int(ok)
    return matched


def crawl_daily_congressional_record(congress):
    matched = 0
    total = _count("daily-congressional-record")
    for item in tqdm(client.api_paginate("daily-congressional-record"), total=total,
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
        try:
            data = client.api_get(f"daily-congressional-record/{volume}/{issue}/articles")
        except Exception:
            continue
        sections = data.get("articles", [])
        for sec_idx, section in enumerate(sections):
            sec_name = section.get("name", f"section{sec_idx}")
            for art_idx, art in enumerate(section.get("sectionArticles", [])):
                formats = _normalize_formats(art.get("text"))
                if not formats:
                    continue
                doc_id = f"CREC-{congress}-V{volume}-I{issue}-{sec_name}-{art_idx}".replace(" ", "_")
                ok = processor.process_document(
                    doc_id=doc_id, collection="congressional-record", congress=congress,
                    document_type="CREC-daily", document_number=f"V{volume}I{issue}",
                    title=art.get("title"), date=item.get("issueDate"),
                    congress_url=item.get("url", ""), formats=formats,
                    api_endpoint=f"/daily-congressional-record/{volume}/{issue}/articles",
                )
                matched += int(ok)
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
    from . import store

    store.counter("bound-congressional-record", congress)
    for year in config.congress_years(congress):
        try:
            # Some years (e.g. 2021-2022 as of 2026-10) return a persistent HTTP 500,
            # so don't spend the full retry/backoff budget on this metadata-only probe.
            listing = client.api_get(f"bound-congressional-record/{year}", {"limit": 1}, max_attempts=2)
        except Exception as e:
            print(f"bound-congressional-record/{year}: API error, not probed ({client.redact_text(str(e))})")
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
                detail = client.api_get(f"bound-congressional-record/{y}/{m}/{d}", {"limit": 1},
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
    start, end = config.congress_date_range(congress)
    total = _count("crsreport")
    matched = 0
    for item in tqdm(client.api_paginate("crsreport"), total=total, desc=f"crsreport (filter congress={congress})", unit="rpt"):
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
        try:
            detail = client.api_get(f"crsreport/{rid}")
        except Exception:
            continue
        rep = detail.get("CRSReport", detail)
        formats = _normalize_formats(rep.get("formats"))
        if not formats:
            continue
        doc_id = f"CRS-{rid}-v{rep.get('version', 1)}"
        ok = processor.process_document(
            doc_id=doc_id, collection="crs-report", congress=congress,
            document_type="CRS", document_number=rid,
            title=rep.get("title"), date=pub,
            congress_url=rep.get("url", ""), formats=formats,
            api_endpoint=f"/crsreport/{rid}",
        )
        matched += int(ok)
    return matched
