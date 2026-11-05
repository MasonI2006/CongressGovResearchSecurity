"""Everything written for a match, plus checks and reports: raw API snapshots,
per-document JSON sidecars, metadata/matches CSV rows, the SQLite database,
local PDF re-verification, archive validation, and the search README."""
import csv
import hashlib
import json
import re
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from pypdf import PdfReader

import core


# ============================================================================
# Metadata capture: raw snapshots, sidecars, CSV rows, SQLite
# ============================================================================

SOURCE_SYSTEM = "congress.gov API"


def matched_terms(meta_row):
    """Terms a metadata row's PDF was saved under (all rows written by this
    version record them; fall back to the search's first term)."""
    try:
        terms = json.loads(meta_row.get("matched_terms") or "[]")
    except json.JSONDecodeError:
        terms = []
    return [t for t in terms if t in core.SEARCH.terms] or [core.SEARCH.terms[0]]


def _db_path():
    return core.ARCHIVE_DIR / "archive.sqlite"


def _safe_component(value):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(value)).strip("._") or "unknown"


def _scrub(value):
    if isinstance(value, dict):
        return {key: _scrub(item) for key, item in value.items() if key.lower() != "api_key"}
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    if isinstance(value, str) and value.startswith(("http://", "https://")):
        return core.sanitize_url(value)
    return value


def _utc_file_time(path):
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _raw_path(collection, stable_id, endpoint):
    return core.RAW_API_DIR / _safe_component(collection) / _safe_component(stable_id) / f"{_safe_component(endpoint)}.json"


def _read_or_fetch(collection, stable_id, endpoint, route, params=None, write_raw=True):
    path = _raw_path(collection, stable_id, endpoint)
    if write_raw and path.exists():
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload, {"http_status": 200, "retrieved_at": _utc_file_time(path), "cached": True,
                         "raw_api_json_path": _relative(path), "endpoint": route}
    payload = core.api_get(route, params)
    response = dict(core.LAST_API_RESPONSE)
    clean_payload = _scrub(payload)
    if write_raw:
        _write_raw_once(collection, stable_id, endpoint, clean_payload)
    return clean_payload, {**response, "raw_api_json_path": _relative(path), "endpoint": core.sanitize_url(route)}


def _relative(path):
    resolved = Path(path).resolve()
    try:
        value = resolved.relative_to(core.ARCHIVE_DIR)
    except ValueError:
        value = resolved
    return str(value).replace("\\", "/")


def _first(payload, key):
    value = payload.get(key) if isinstance(payload, dict) else None
    if isinstance(value, list):
        return value[0] if value else {}
    return value if isinstance(value, dict) else {}


def _fetch_paged(collection, stable_id, endpoint, route, result_key):
    payloads = []
    all_items = []
    page_no = 0
    next_route = route
    while True:
        endpoint_name = endpoint if page_no == 0 else f"{endpoint}_page_{page_no}"
        params = {"limit": core.PAGE_SIZE, "offset": 0} if page_no == 0 else None
        payload, retrieval = _read_or_fetch(collection, stable_id, endpoint_name, next_route, params)
        payloads.append((endpoint_name, payload, retrieval))
        items = payload.get(result_key) or []
        all_items.extend(items)
        pagination = payload.get("pagination") or {}
        if not pagination.get("next") or not items:
            break
        next_route = pagination["next"]
        page_no += 1
    return all_items, payloads


def _parse_identity(collection, document_id, congress, document_type, document_number):
    if collection == "bill":
        match = re.match(r"BILL-(\d+)-([A-Za-z]+)-(\d+)-", document_id)
        if match:
            c, kind, number = match.groups()
        else:
            c, kind, number = str(congress), str(document_type), str(document_number)
        root = f"bill/{c}/{kind.lower()}/{number}"
        return f"{c}-{kind.upper()}-{number}", root, {"detail": root, "text": root + "/text"}
    if collection == "amendment":
        match = re.match(r"AMDT-(\d+)-([A-Za-z]+)-(\d+)-", document_id)
        if not match:
            raise ValueError(f"Cannot parse amendment id {document_id}")
        c, kind, number = match.groups()
        root = f"amendment/{c}/{kind.lower()}/{number}"
        return f"{c}-{kind.upper()}-{number}", root, {"detail": root, "text": root + "/text", "actions": root + "/actions"}
    if collection == "committee-report":
        match = re.match(r"RPT-(\d+)-([A-Za-z]+)-(\d+)-", document_id)
        if not match:
            raise ValueError(f"Cannot parse report id {document_id}")
        c, kind, number = match.groups()
        root = f"committee-report/{c}/{kind.lower()}/{number}"
        citation_type = {"HRPT": "H. Rept.", "SRPT": "S. Rept.", "ERPT": "Ex. Rept."}.get(kind.upper())
        stable = f"{citation_type} {c}-{number}" if citation_type else f"{c}-{kind.upper()}-{number}"
        return stable, root, {"detail": root, "text": root + "/text"}
    if collection == "committee-print":
        match = re.match(r"CPRT-(\d+)-([A-Za-z]+)-(\d+)", document_id)
        if not match:
            raise ValueError(f"Cannot parse committee print id {document_id}")
        c, chamber, jacket = match.groups()
        root = f"committee-print/{c}/{chamber.lower()}/{jacket}"
        return f"{c}-{chamber.lower()}-{jacket}", root, {"detail": root, "text": root + "/text"}
    if collection == "hearing":
        match = re.match(r"HRG-(\d+)-([A-Za-z]+)-(\d+)", document_id)
        if not match:
            raise ValueError(f"Cannot parse hearing id {document_id}")
        c, chamber, jacket = match.groups()
        root = f"hearing/{c}/{chamber.lower()}/{jacket}"
        return f"{c}-{chamber.lower()}-{jacket}", root, {"detail": root}
    if collection == "committee-meeting":
        match = re.match(r"MTG-(\d+)-([A-Za-z]+)-(\d+)-(\d+)-", document_id)
        if not match:
            raise ValueError(f"Cannot parse meeting document id {document_id}")
        c, chamber, event_id, doc_index = match.groups()
        root = f"committee-meeting/{c}/{chamber.lower()}/{event_id}"
        return f"{c}-{chamber.lower()}-{event_id}__{document_id}", root, {"detail": root}, int(doc_index)
    if collection == "congressional-record":
        match = re.match(r"CREC-(\d+)-V(\d+)-I(\d+)-", document_id)
        if not match:
            raise ValueError(f"Cannot parse Congressional Record id {document_id}")
        c, volume, issue = match.groups()
        root = f"daily-congressional-record/{volume}/{issue}"
        return f"CR-{volume}-{issue}-pending", root, {"issue": root, "articles": root + "/articles"}
    if collection == "crs-report":
        match = re.match(r"CRS-([A-Za-z0-9]+)(?:-v\d+)?", document_id)
        if not match:
            raise ValueError(f"Cannot parse CRS id {document_id}")
        product_id = match.group(1)
        root = f"crsreport/{product_id}"
        return f"CRS-{product_id}", root, {"detail": root}
    if collection == "bound-congressional-record":
        date = str(document_number)
        year = date[:4]
        root = f"bound-congressional-record/{year}"
        return f"BCREC-{congress}-{date}", root, {"detail": root}
    raise ValueError(f"Unsupported collection {collection}")


def _get_data(collection, stable_id, document_id, congress, document_type, document_number, pdf_url):
    raw_stable_id, root, routes, meeting_doc_index = _parse_identity(
        collection, document_id, congress, document_type, document_number
    ) if collection == "committee-meeting" else (*_parse_identity(
        collection, document_id, congress, document_type, document_number
    ), None)
    payloads = {}
    retrievals = []
    if collection == "bill":
        detail, retrieval = _read_or_fetch(collection, stable_id, "detail", routes["detail"])
        payloads["detail"] = detail; retrievals.append(retrieval)
        text, retrieval = _read_or_fetch(collection, stable_id, "text", routes["text"])
        payloads["text"] = text; retrievals.append(retrieval)
        for endpoint, result_key in (("cosponsors", "cosponsors"),
                         ("committees", "committees"), ("relatedbills", "relatedBills")):
            items, pages = _fetch_paged(collection, stable_id, endpoint, root + "/" + endpoint, result_key)
            payloads[endpoint] = {result_key: items, "pagination": {"count": len(items)}}
            retrievals.extend(page[2] for page in pages)
        payloads["subjects"], retrieval = _read_or_fetch(collection, stable_id, "subjects", root + "/subjects")
        retrievals.append(retrieval)
    elif collection == "amendment":
        for endpoint, route in routes.items():
            payload, retrieval = _read_or_fetch(collection, stable_id, endpoint, route)
            payloads[endpoint] = payload; retrievals.append(retrieval)
    elif collection in ("committee-report", "committee-print"):
        for endpoint, route in routes.items():
            payload, retrieval = _read_or_fetch(collection, stable_id, endpoint, route)
            payloads[endpoint] = payload; retrievals.append(retrieval)
    elif collection == "hearing":
        payload, retrieval = _read_or_fetch(collection, stable_id, "detail", routes["detail"])
        payloads["detail"] = payload; retrievals.append(retrieval)
        hearing = _first(payload, "hearing")
        associated = hearing.get("associatedMeeting")
        if isinstance(associated, dict):
            associated = associated.get("url") or associated.get("URL")
        if isinstance(associated, str):
            linked = re.search(r"/committee-meeting/(\d+)/(house|senate|nochamber)/([^/?]+)", associated, re.I)
            if linked:
                c, chamber, event_id = linked.groups()
                route = f"committee-meeting/{c}/{chamber.lower()}/{event_id}"
                payloads["associated_meeting"], retrieval = _read_or_fetch(
                    collection, stable_id, "associated_meeting", route
                )
                retrievals.append(retrieval)
    elif collection == "committee-meeting":
        payload, retrieval = _read_or_fetch(collection, stable_id, "detail", routes["detail"])
        payloads["detail"] = payload; retrievals.append(retrieval)
        payloads["matched_document_index"] = meeting_doc_index
    elif collection == "congressional-record":
        for endpoint, route in routes.items():
            payload, retrieval = _read_or_fetch(collection, stable_id, endpoint, route, write_raw=False)
            payloads[endpoint] = payload; retrievals.append(retrieval)
    elif collection == "crs-report":
        payload, retrieval = _read_or_fetch(collection, stable_id, "detail", routes["detail"])
        payloads["detail"] = payload; retrievals.append(retrieval)
    elif collection == "bound-congressional-record":
        payload, retrieval = _read_or_fetch(collection, stable_id, "detail", routes["detail"])
        payloads["detail"] = payload; retrievals.append(retrieval)

    api_fields, api_object = _normalize(collection, payloads, pdf_url, document_id)
    if collection == "committee-report":
        api_object = _first(payloads["detail"], "committeeReports")
        stable_id = api_object.get("citation") or raw_stable_id
    elif collection == "congressional-record":
        issue = _first(payloads["issue"], "issue")
        article, section = _match_record_article(payloads["articles"], pdf_url)
        if article:
            stable_id = f"CR-{issue.get('volumeNumber')}-{issue.get('issueNumber')}-{section.get('name')}-{article.get('startPage')}"
            api_fields, api_object = _normalize(collection, payloads, pdf_url, document_id)
            api_fields.update({"section": section.get("name"), "articleTitle": article.get("title"),
                               "startPage": article.get("startPage"), "endPage": article.get("endPage")})
            api_object = issue
        for retrieval in retrievals:
            endpoint_name = "issue" if retrieval.get("endpoint") == routes["issue"] else "articles"
            retrieval["raw_api_json_path"] = _relative(_raw_path(collection, stable_id, endpoint_name))
    else:
        api_object = _detail_object(collection, payloads)
    if collection == "committee-meeting":
        api_object = _first(payloads["detail"], "committeeMeeting")
    if collection == "hearing" and "associated_meeting" in payloads:
        _write_raw_once(collection, stable_id, "associated_meeting", payloads["associated_meeting"])
    if collection == "committee-meeting":
        _write_raw_once(collection, stable_id, "detail", payloads["detail"])
    if collection == "bill":
        for endpoint in ("detail", "text", "cosponsors", "subjects", "committees", "relatedbills"):
            _write_raw_once(collection, stable_id, endpoint, payloads[endpoint])
    if collection == "amendment":
        for endpoint in routes:
            _write_raw_once(collection, stable_id, endpoint, payloads[endpoint])
    if collection in ("committee-report", "committee-print", "congressional-record"):
        for endpoint in routes:
            _write_raw_once(collection, stable_id, endpoint, payloads[endpoint])
    if collection in ("hearing", "crs-report", "bound-congressional-record"):
        _write_raw_once(collection, stable_id, "detail", payloads.get("detail") or {})

    detail_retrieval = next((item for item in retrievals if item.get("endpoint") == routes.get("detail") or
                             item.get("endpoint") == routes.get("issue")), retrievals[0] if retrievals else {})
    return {
        "stable_id": stable_id,
        "raw_stable_id": raw_stable_id,
        "routes": routes,
        "payloads": payloads,
        "retrievals": retrievals,
        "api_fields": api_fields,
        "api_object": api_object,
        "http_status": detail_retrieval.get("http_status", 200),
        "retrieved_at": detail_retrieval.get("retrieved_at") or core._utc_now(),
        "main_raw_path": _relative(_raw_path(collection, stable_id,
                                               "detail" if collection != "congressional-record" else "issue")),
    }


def _write_raw_once(collection, stable_id, endpoint, payload):
    path = _raw_path(collection, stable_id, endpoint)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return _relative(path)
    clean_payload = _scrub(payload)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(clean_payload, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    try:
        temp.replace(path)
    except FileExistsError:
        temp.unlink(missing_ok=True)
    return _relative(path)


def _detail_object(collection, payloads):
    root_keys = {
        "bill": "bill", "amendment": "amendment", "committee-report": "committeeReports",
        "committee-print": "committeePrint", "hearing": "hearing",
        "committee-meeting": "committeeMeeting", "crs-report": "CRSReport",
        "bound-congressional-record": "boundCongressionalRecord",
    }
    payload = payloads.get("detail") or {}
    return _first(payload, root_keys.get(collection, ""))


def _match_record_article(payload, pdf_url):
    target = core.sanitize_url(pdf_url)
    for section in payload.get("articles", []) or []:
        for article in section.get("sectionArticles", []) or []:
            if any(core.sanitize_url(fmt.get("url", "")) == target for fmt in article.get("text", []) or []):
                return article, section
    return {}, {}


def _normalize(collection, payloads, pdf_url, document_id):
    if collection == "bill":
        bill = _first(payloads.get("detail", {}), "bill")
        cosponsors = payloads.get("cosponsors", {}).get("cosponsors") or []
        subjects = payloads.get("subjects", {}).get("subjects") or {}
        committees = payloads.get("committees", {}).get("committees") or []
        related = payloads.get("relatedbills", {}).get("relatedBills") or []
        laws = bill.get("laws") or []
        versions = (payloads.get("text") or {}).get("textVersions") or []
        version = next((v for v in versions if any(core.sanitize_url(f.get("url", "")) == core.sanitize_url(pdf_url)
                                                    for f in v.get("formats", []) or [])), None)
        return {
            "type": bill.get("type"), "number": bill.get("number"),
            "originChamber": bill.get("originChamber"), "introducedDate": bill.get("introducedDate"),
            "title": bill.get("title"),
            "latestAction": {"text": (bill.get("latestAction") or {}).get("text"),
                             "date": (bill.get("latestAction") or {}).get("actionDate")},
            "policyArea": {"name": (bill.get("policyArea") or {}).get("name")},
            "sponsors": [{"bioguideId": p.get("bioguideId"), "name": p.get("fullName"),
                          "party": p.get("party"), "state": p.get("state")} for p in bill.get("sponsors", []) or []],
            "cosponsorCount": (payloads.get("cosponsors", {}).get("pagination") or {}).get("count", len(cosponsors)),
            "cosponsorBioguideIds": [p.get("bioguideId") for p in cosponsors if isinstance(p, dict)],
            "legislativeSubjects": subjects.get("legislativeSubjects") or [],
            "committees": [{"name": c.get("name"), "systemCode": c.get("systemCode")} for c in committees],
            "textVersion": {"type": version.get("type"), "date": version.get("date")} if version else None,
            "publicLawNumber": next((law.get("number") for law in laws if isinstance(law, dict)), None),
            "laws": laws, "relatedBills": related,
            "updateDateIncludingText": bill.get("updateDateIncludingText"),
        }, bill
    if collection == "amendment":
        item = _first(payloads.get("detail", {}), "amendment")
        acts = payloads.get("actions", {}).get("actions") or []
        dated = sorted((a for a in acts if a.get("actionDate")), key=lambda a: a.get("actionDate", ""))
        latest = dated[-1] if dated else None
        return {
            "type": item.get("type"), "number": item.get("number"), "chamber": item.get("chamber"),
            "description": None, "purpose": None, "submittedDate": item.get("submittedDate"),
            "latestAction": {"text": None, "date": latest.get("actionDate") if latest else None,
                             "actionCode": latest.get("actionCode") if latest else None,
                             "type": latest.get("type") if latest else None},
            "sponsors": [{"bioguideId": p.get("bioguideId")} for p in item.get("sponsors", []) or []],
            "amendedBill": ({key: item["amendedBill"].get(key)
                             for key in ("congress", "type", "number", "originChamber", "title")}
                            if isinstance(item.get("amendedBill"), dict) else None),
        }, item
    if collection == "committee-report":
        item = _first(payloads.get("detail", {}), "committeeReports")
        return {
            "citation": item.get("citation"), "type": item.get("type"), "number": item.get("number"),
            "part": item.get("part"), "chamber": item.get("chamber"), "title": item.get("title"),
            "issueDate": item.get("issueDate"), "isConferenceReport": item.get("isConferenceReport"),
            "associated_bills": item.get("associatedBill") or [], "reportType": item.get("reportType"),
            "sessionNumber": item.get("sessionNumber"),
        }, item
    if collection == "committee-print":
        item = _first(payloads.get("detail", {}), "committeePrint")
        return {
            "jacketNumber": item.get("jacketNumber"), "number": item.get("number"),
            "chamber": item.get("chamber"), "title": item.get("title"), "citation": item.get("citation"),
            "committees": [{"name": c.get("name"), "systemCode": c.get("systemCode")}
                           for c in item.get("committees", []) or []],
            "associatedBills": item.get("associatedBills") or [],
        }, item
    if collection == "hearing":
        item = _first(payloads.get("detail", {}), "hearing")
        linked_payload = payloads.get("associated_meeting") or {}
        meeting = _first(linked_payload, "committeeMeeting")
        assoc = item.get("associatedMeeting")
        event_id = meeting.get("eventId")
        if event_id is None and isinstance(assoc, dict):
            event_id = assoc.get("eventId")
        return {
            "jacketNumber": item.get("jacketNumber"), "chamber": item.get("chamber"),
            "title": item.get("title"), "citation": item.get("citation"), "dates": item.get("dates") or [],
            "committees": item.get("committees") or [],
            "libraryOfCongressIdentifier": item.get("libraryOfCongressIdentifier"),
            "formats": item.get("formats") or [], "associatedMeeting": {"eventId": event_id} if event_id else None,
            "linkedMeetingWitnesses": meeting.get("witnesses") or [],
        }, item
    if collection == "committee-meeting":
        item = _first(payloads.get("detail", {}), "committeeMeeting")
        doc_index = payloads.get("matched_document_index")
        combined = [("meetingDocuments", d) for d in (item.get("meetingDocuments") or [])]
        combined += [("witnessDocuments", d) for d in (item.get("witnessDocuments") or [])]
        matched_kind, matched = (combined[doc_index] if isinstance(doc_index, int) and doc_index < len(combined)
                                 else (None, {}))
        return {
            "eventId": item.get("eventId"), "title": item.get("title"), "type": item.get("type"),
            "meetingStatus": item.get("meetingStatus"), "date": item.get("date"),
            "chamber": item.get("chamber"), "committees": item.get("committees") or [],
            "location": item.get("location"), "relatedItems": item.get("relatedItems") or [],
            "meetingDocuments": item.get("meetingDocuments") or [], "witnesses": item.get("witnesses") or [],
            "witnessDocuments": item.get("witnessDocuments") or [],
            "matchedDocument": {"source": matched_kind, "documentType": matched.get("documentType"),
                                "name": matched.get("name"), "url": core.sanitize_url(matched.get("url", ""))},
        }, item
    if collection == "congressional-record":
        issue = _first(payloads.get("issue", {}), "issue")
        article, section = _match_record_article(payloads.get("articles") or {}, pdf_url)
        return {
            "volumeNumber": issue.get("volumeNumber"), "issueNumber": issue.get("issueNumber"),
            "issueDate": issue.get("issueDate"), "sessionNumber": issue.get("sessionNumber"),
            "congress": issue.get("congress"), "section": section.get("name"),
            "articleTitle": article.get("title"), "startPage": article.get("startPage"),
            "endPage": article.get("endPage"),
        }, issue
    if collection == "crs-report":
        item = _first(payloads.get("detail", {}), "CRSReport")
        return {
            "id": item.get("id"), "title": item.get("title"), "contentType": item.get("contentType"),
            "publishDate": item.get("publishDate"), "updateDate": item.get("updateDate"),
            "authors": item.get("authors") or [], "topics": item.get("topics") or [],
            "summary": item.get("summary"), "currentVersion": item.get("currentVersion"),
            "relatedMaterials": item.get("relatedMaterials"),
            "congressBasis": "publish-date range; CRS product API has no congress field",
        }, item
    if collection == "bound-congressional-record":
        issue = _first(payloads.get("detail", {}), "boundCongressionalRecord")
        return {"date": issue.get("date"), "volumeNumber": issue.get("volumeNumber"),
                "congress": issue.get("congress")}, issue
    raise ValueError(f"Unsupported collection {collection}")


def _pdf_stats(pdf_bytes):
    page_count = 0
    has_text = False
    try:
        reader = PdfReader(__import__("io").BytesIO(pdf_bytes))
        if reader.is_encrypted:
            reader.decrypt("")
        page_count = len(reader.pages)
        for page in reader.pages:
            try:
                if (page.extract_text() or "").strip():
                    has_text = True
                    break
            except Exception:
                continue
    except Exception:
        pass
    return page_count, has_text


def _normalize_text_source(source):
    kind = (source or "").strip().lower()
    if kind in ("formatted text", "formatted_text"):
        return "formatted_text"
    if "xml" in kind:
        return "xml"
    if "html" in kind or "generated html" in kind:
        return "html"
    return "pdf_extraction"


def _people_and_committees(collection, payloads, api_fields, api_object):
    people = []
    committees = []
    if collection == "bill":
        people.extend({"role": "sponsor", "bioguideId": p.get("bioguideId"), "name": p.get("fullName"),
                       "party": p.get("party"), "state": p.get("state")}
                      for p in api_object.get("sponsors", []) or [])
        people.extend({"role": "cosponsor", "bioguideId": p.get("bioguideId"), "name": p.get("fullName"),
                       "party": p.get("party"), "state": p.get("state")}
                      for p in payloads.get("cosponsors", {}).get("cosponsors", []) or [])
        committees = payloads.get("committees", {}).get("committees", []) or []
    elif collection == "amendment":
        people.extend({"role": "sponsor", "bioguideId": p.get("bioguideId"), "name": p.get("fullName"),
                       "party": p.get("party"), "state": p.get("state")}
                      for p in api_object.get("sponsors", []) or [])
    elif collection in ("committee-report", "committee-print", "hearing"):
        committees = api_object.get("committees", []) or []
    elif collection == "committee-meeting":
        committees = api_object.get("committees", []) or []
        people.extend({"role": "witness", "bioguideId": None, "name": p.get("name"),
                       "party": None, "state": p.get("organization"), "position": p.get("position")}
                      for p in api_object.get("witnesses", []) or [])
    return people, committees


def _related_items(collection, api_fields, api_object):
    related = []
    def append(relation, items):
        if isinstance(items, dict):
            items = [items]
        for item in items or []:
            if not isinstance(item, dict):
                continue
            c, kind, number = item.get("congress"), item.get("type"), item.get("number")
            key = f"{c}-{kind}-{number}" if c is not None and kind and number is not None else None
            if key is None and item.get("eventId") is not None:
                key = str(item.get("eventId"))
            if key is None and (item.get("url") or item.get("URL")):
                key = core.sanitize_url(item.get("url") or item.get("URL"))
            related.append({"relation_type": relation, "related_key": key, "payload": _scrub(item)})
    append("related_bill", api_fields.get("relatedBills"))
    append("associated_bill", api_fields.get("associated_bills"))
    append("associated_bill", api_fields.get("associatedBills"))
    append("amended_bill", api_fields.get("amendedBill"))
    append("related_item", api_fields.get("relatedItems"))
    append("law", api_fields.get("laws"))
    associated_meeting = api_fields.get("associatedMeeting") or {}
    if associated_meeting.get("eventId"):
        append("associated_meeting", [{"eventId": associated_meeting["eventId"]}])
    return related


def _initialize_db(db):
    db.executescript("""
    PRAGMA foreign_keys=ON;
    CREATE TABLE IF NOT EXISTS documents (
      stable_id TEXT PRIMARY KEY, collection TEXT NOT NULL, congress TEXT, title TEXT,
      update_date TEXT, source_system TEXT NOT NULL, api_base TEXT NOT NULL,
      api_detail_url TEXT, retrieved_at TEXT NOT NULL, http_status INTEGER,
      raw_api_json_path TEXT, api_fields_json TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS files (
      pdf_sha256 TEXT NOT NULL, stable_id TEXT NOT NULL, filename TEXT NOT NULL,
      pdf_bytes INTEGER, pdf_page_count INTEGER, pdf_has_text_layer INTEGER,
        text_source_used TEXT, pdf_url TEXT, sidecar_path TEXT NOT NULL,
      PRIMARY KEY(pdf_sha256, stable_id)
    );
    CREATE TABLE IF NOT EXISTS retrievals (
      stable_id TEXT NOT NULL, endpoint TEXT NOT NULL, raw_api_json_path TEXT NOT NULL,
      retrieved_at TEXT NOT NULL, http_status INTEGER, cached INTEGER,
      PRIMARY KEY(stable_id, endpoint, raw_api_json_path)
    );
    CREATE TABLE IF NOT EXISTS matches (
      match_id TEXT PRIMARY KEY, stable_id TEXT NOT NULL, pdf_sha256 TEXT NOT NULL,
      filename TEXT NOT NULL, page_number TEXT, character_offset TEXT,
      context TEXT NOT NULL, text_source_used TEXT, matched_surface_form TEXT, term TEXT
    );
    CREATE TABLE IF NOT EXISTS people (
      stable_id TEXT NOT NULL, bioguide_id TEXT, role TEXT NOT NULL, name TEXT,
      party TEXT, state TEXT, position TEXT, PRIMARY KEY(stable_id, bioguide_id, role, name)
    );
    CREATE TABLE IF NOT EXISTS committees (
      stable_id TEXT NOT NULL, system_code TEXT, name TEXT,
      PRIMARY KEY(stable_id, system_code, name)
    );
    CREATE TABLE IF NOT EXISTS related_items (
      stable_id TEXT NOT NULL, relation_type TEXT NOT NULL, related_key TEXT,
      payload_json TEXT NOT NULL, PRIMARY KEY(stable_id, relation_type, related_key)
    );
    """)


_MATCH_INSERT = ("INSERT OR IGNORE INTO matches (match_id, stable_id, pdf_sha256, filename, page_number, "
                 "character_offset, context, text_source_used, matched_surface_form, term) "
                 "VALUES(?,?,?,?,?,?,?,?,?,?)")


def _write_database(record, match_rows):
    db_path = _db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as db:
        _initialize_db(db)
        db.execute("BEGIN IMMEDIATE")
        fields = record["api_fields"]
        common = record["common"]
        db.execute("""INSERT INTO documents VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(stable_id) DO UPDATE SET collection=excluded.collection, congress=excluded.congress,
          title=excluded.title, update_date=excluded.update_date, source_system=excluded.source_system,
          api_base=excluded.api_base, api_detail_url=excluded.api_detail_url, retrieved_at=excluded.retrieved_at,
          http_status=excluded.http_status, raw_api_json_path=excluded.raw_api_json_path,
          api_fields_json=excluded.api_fields_json""",
          (common["stable_id"], common["collection"], str(common.get("congress") or ""), common.get("title"),
           common.get("update_date"), SOURCE_SYSTEM, core.API_BASE, common["api_detail_url"],
           common["retrieved_at"], common["http_status"], common["raw_api_json_path"],
           json.dumps(_scrub(fields), ensure_ascii=False)))
        db.execute("""INSERT INTO files VALUES(?,?,?,?,?,?,?,?,?)
          ON CONFLICT(pdf_sha256,stable_id) DO UPDATE SET filename=excluded.filename,
          pdf_bytes=excluded.pdf_bytes,pdf_page_count=excluded.pdf_page_count,
          pdf_has_text_layer=excluded.pdf_has_text_layer,text_source_used=excluded.text_source_used,
          pdf_url=excluded.pdf_url,sidecar_path=excluded.sidecar_path""",
          (common["pdf_sha256"], common["stable_id"], record["filename"], common["pdf_bytes"],
           common["pdf_page_count"], int(common["pdf_has_text_layer"]), common["text_source_used"],
           common["pdf_url"], record["sidecar_path"]))
        for retrieval in record["retrievals"]:
            db.execute("INSERT OR REPLACE INTO retrievals VALUES(?,?,?,?,?,?)",
                       (common["stable_id"], retrieval.get("endpoint", ""), retrieval["raw_api_json_path"],
                        retrieval["retrieved_at"], retrieval["http_status"], int(retrieval.get("cached", False))))
        for person in record["people"]:
            db.execute("INSERT OR REPLACE INTO people VALUES(?,?,?,?,?,?,?)",
                       (common["stable_id"], person.get("bioguideId"), person.get("role", "person"),
                        core.clean_text(person.get("name")), person.get("party"), person.get("state"), person.get("position")))
        for committee in record["committees"]:
            db.execute("INSERT OR REPLACE INTO committees VALUES(?,?,?)",
                       (common["stable_id"], committee.get("systemCode"), committee.get("name")))
        for related in record["related_items"]:
            db.execute("INSERT OR REPLACE INTO related_items VALUES(?,?,?,?)",
                       (common["stable_id"], related["relation_type"], related.get("related_key"),
                        json.dumps(_scrub(related["payload"]), ensure_ascii=False, sort_keys=True)))
        for match in match_rows:
            db.execute(_MATCH_INSERT,
                       (match["match_id"], common["stable_id"], common["pdf_sha256"], record["filename"],
                        core.clean_text(match.get("page_number")), core.clean_text(match.get("character_offset")),
                        core.clean_text(match["matching_text_context"]), common["text_source_used"],
                        match.get("matched_surface_form"), match.get("term")))
        db.commit()


def insert_match_rows(match_rows):
    """Add complete matches.csv-shaped rows to the SQLite matches table (idempotent by match_id)."""
    if not match_rows:
        return
    with sqlite3.connect(_db_path()) as db:
        _initialize_db(db)
        for match in match_rows:
            db.execute(_MATCH_INSERT,
                       (match["match_id"], match["stable_id"], match["pdf_sha256"], match["filename"],
                        core.clean_text(match.get("page_number")), core.clean_text(match.get("character_offset")),
                        core.clean_text(match["matching_text_context"]), match.get("text_source_used"),
                        match.get("matched_surface_form") or None, match.get("term")))
        db.commit()


def _sidecar_path(collection, stable_id, document_id):
    directory = core.ARCHIVE_DIR / "metadata" / _safe_component(collection)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{_safe_component(stable_id)}.json"
    if path.exists():
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
            if current.get("document_identifier") == document_id:
                return path
        except (OSError, json.JSONDecodeError):
            pass
        path = directory / f"{_safe_component(stable_id)}__{_safe_component(document_id)}.json"
    return path


def _selected_source(formats, used_text_type):
    if used_text_type:
        return _normalize_text_source(used_text_type)
    for fmt in formats or []:
        kind = fmt.get("type") or fmt.get("format")
        if kind and str(kind).upper() == "PDF":
            continue
        if kind:
            return _normalize_text_source(kind)
    return "pdf_extraction"


def _format_list(collection, data, pdf_url, document_id):
    if collection in ("bill", "amendment"):
        for version in (data.get("text", {}).get("textVersions") or []):
            if any(core.sanitize_url(fmt.get("url", "")) == core.sanitize_url(pdf_url)
                   for fmt in version.get("formats", []) or []):
                return version.get("formats") or [], {"type": version.get("type"), "date": version.get("date")}
        return [], None
    if collection == "committee-report":
        entries = data.get("text", {}).get("text") or []
        formats = [fmt for entry in entries for fmt in (entry.get("formats") or [])]
        return formats, None
    if collection == "committee-print":
        return data.get("text", {}).get("text") or [], None
    if collection == "hearing":
        item = _first(data.get("detail", {}), "hearing")
        return item.get("formats") or [], None
    if collection == "congressional-record":
        article, _section = _match_record_article(data.get("articles", {}), pdf_url)
        return article.get("text") or [], None
    if collection == "crs-report":
        item = _first(data.get("detail", {}), "CRSReport")
        return item.get("formats") or [], {"currentVersion": item.get("currentVersion")}
    return [], None


def _match_rows(matches, stable_id, pdf_sha, document_id, collection, congress, filename, text_source,
                api_endpoint):
    rows = []
    for page, context, offset, term, surface in matches:
        rows.append({
            "filename": filename, "document_identifier": document_id, "collection": collection,
            "congress": congress if congress is not None else "",
            "page_number": page if page is not None else "", "matching_text_context": core.clean_text(context),
            "char_location": offset if offset is not None else "", "api_endpoint": api_endpoint,
            "stable_id": stable_id, "pdf_sha256": pdf_sha,
            "character_offset": offset if offset is not None else "", "text_source_used": text_source,
            "matched_surface_form": surface, "term": term,
            "match_id": core.match_id(stable_id, pdf_sha, document_id, page, offset,
                                       core.clean_text(context), term),
        })
    return rows


def capture_match(collection, document_id, congress, document_type, document_number, title,
                  document_date, congress_url, api_endpoint, pdf_url, pdf_bytes, formats,
                  used_text_type, matches, filename, matched_terms):
    identity = _parse_identity(collection, document_id, congress, document_type, document_number)
    provisional_id, detail_route = identity[:2]
    data = _get_data(collection, provisional_id, document_id, congress, document_type, document_number, pdf_url)
    stable_id = data["stable_id"]
    api_fields = data["api_fields"]
    api_object = data["api_object"]
    if collection == "amendment":
        title = api_object.get("title")
    else:
        title = api_object.get("title") or api_fields.get("title") or title
    update_date = api_object.get("updateDate") or api_object.get("updateDateIncludingText")
    if collection == "crs-report":
        update_date = api_object.get("updateDate")
    if collection == "congressional-record":
        update_date = api_object.get("updateDate")
    if collection == "committee-report":
        update_date = api_object.get("updateDate")
    if collection == "committee-print":
        update_date = api_object.get("updateDate")

    pdf_sha = hashlib.sha256(pdf_bytes).hexdigest()
    page_count, has_text = _pdf_stats(pdf_bytes)
    source = _selected_source(formats, used_text_type)
    detail_route = data["routes"].get("detail") or data["routes"].get("issue") or detail_route
    detail_url = core.sanitize_url(f"{core.API_BASE}/{detail_route}")
    raw_path = data["main_raw_path"]
    common = {
        "stable_id": stable_id, "collection": collection,
        "congress": None if collection == "crs-report" else
                (api_object.get("congress") or api_fields.get("congress") or congress),
        "source_system": SOURCE_SYSTEM, "api_base": core.API_BASE,
        "api_detail_url": detail_url, "pdf_url": core.sanitize_url(pdf_url),
        "retrieved_at": data["retrieved_at"], "http_status": data["http_status"],
        "pdf_sha256": pdf_sha, "pdf_bytes": len(pdf_bytes), "pdf_page_count": page_count,
        "pdf_has_text_layer": has_text, "text_source_used": source,
        "raw_api_json_path": raw_path, "title": title,
        "update_date": update_date,
    }
    people, committees = _people_and_committees(collection, data["payloads"], api_fields, api_object)
    related_items = _related_items(collection, api_fields, api_object)
    sidecar_path = _sidecar_path(collection, stable_id, document_id)
    sidecar_rel = _relative(sidecar_path)
    all_match_rows = _match_rows(matches, stable_id, pdf_sha, document_id, collection, common["congress"],
                                 filename, source, api_endpoint)
    related_locations = [{"stable_id": stable_id, "collection": collection, "congress": common["congress"],
                          "document_type": document_type, "document_number": document_number,
                          "api_detail_url": detail_url, "pdf_url": core.sanitize_url(pdf_url),
                          "sidecar_path": sidecar_rel}]
    metadata_row = {
        "filename": filename, "sha256": pdf_sha,
        "collection": collection, "congress": common["congress"],
        "document_type": document_type, "document_number": document_number,
        "title": title, "date": document_date, "congress_url": core.sanitize_url(congress_url),
        "pdf_url": core.sanitize_url(pdf_url), **common,
        "api_fields_json": json.dumps(_scrub(api_fields), ensure_ascii=False),
        "related_document_ids": json.dumps([document_id]),
        "related_locations_json": json.dumps(related_locations, ensure_ascii=False),
        "sidecar_path": sidecar_rel,
        "matched_terms": json.dumps(matched_terms, ensure_ascii=False),
    }
    sidecar = {
        **common, "document_type": document_type, "document_number": document_number,
        "document_identifier": document_id, "matched_terms": matched_terms, "text_version": _format_list(collection, data["payloads"], pdf_url, document_id)[1],
        **_scrub(api_fields), "people": people, "committees": committees,
        "related_items": related_items,
        "retrievals": data["retrievals"], "sidecar_path": sidecar_rel,
        "known_limitations": (["Congress number is not present in CRS product API; publish-date basis used."]
                               if collection == "crs-report" else []),
    }
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    if not sidecar_path.exists():
        temp = sidecar_path.with_suffix(".json.tmp")
        with temp.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(_scrub(sidecar), stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        temp.replace(sidecar_path)

    # Each persistence layer is idempotent; processed state is written by processor only after this returns.
    core.upsert_metadata(metadata_row)
    core.upsert_matches(all_match_rows)
    record = {
        "common": common, "api_fields": api_fields, "api_object": api_object,
        "collection": collection, "filename": metadata_row["filename"],
        "sidecar_path": sidecar_rel, "retrievals": data["retrievals"],
        "people": people, "committees": committees, "related_items": related_items,
    }
    _write_database(record, all_match_rows)
    return {"stable_id": stable_id, "pdf_sha256": pdf_sha, "filename": metadata_row["filename"],
            "sidecar_path": sidecar_rel, "text_source_used": source, "match_rows": all_match_rows}


# ============================================================================
# Local PDF verification
# ============================================================================

VERIFY_ENDPOINT = "local_pdf_verification"
VERIFY_CONTEXT = "(confirmed via local PDF re-extraction pass)"


def _complete_row(row, meta):
    """Fill the columns a crawl-time match row has (stable_id, pdf_sha256,
    match_id, ...) so verification rows match matches.csv and SQLite."""
    stable_id = row.get("stable_id") or meta.get("stable_id") or row["document_identifier"]
    sha = row.get("pdf_sha256") or meta.get("pdf_sha256") or meta.get("sha256")
    page = row.get("page_number") or ""
    context = row.get("matching_text_context") or VERIFY_CONTEXT
    term = row.get("term") or matched_terms(meta)[0]
    return {**row, "stable_id": stable_id, "pdf_sha256": sha, "page_number": page,
            "matching_text_context": context, "char_location": "", "character_offset": "",
            "text_source_used": "pdf_extraction", "matched_surface_form": "", "term": term,
            "match_id": row.get("match_id") or core.match_id(stable_id, sha, row["document_identifier"],
                                                              page, None, context, term)}


def run_local_verification():
    metadata_path = core.ARCHIVE_DIR / "metadata.csv"
    matches_path = core.ARCHIVE_DIR / "matches.csv"
    if not metadata_path.exists():
        return {"checked": 0, "confirmed": 0, "discrepancies": []}

    with open(metadata_path, newline="", encoding="utf-8-sig") as f:
        pdf_rows = list(csv.DictReader(f))

    matches_by_file = {}
    if matches_path.exists():
        with open(matches_path, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                matches_by_file.setdefault(row["filename"], []).append(row)

    discrepancies = []
    confirmed = 0
    extra_page_rows = []

    for row in pdf_rows:
        fname = row["filename"]
        terms = matched_terms(row)
        path = core.term_pdf_dir(terms[0]) / fname
        if not path.exists():
            discrepancies.append({"filename": fname, "issue": "file missing on disk"})
            continue
        pages = core.extract_pdf_pages_text(path.read_bytes())
        pages_by_term = {term: [] for term in terms}
        for page_num, page_text in pages:
            for term in core.terms_in(page_text) & set(terms):
                pages_by_term[term].append(page_num)
        found_terms = core.terms_in("\n".join(t for _, t in pages)) | {t for t in terms if pages_by_term[t]}
        if not set(terms) <= found_terms:
            fallback_pages = core._pdftotext_pages(path.read_bytes())
            found_terms |= core.terms_in("\n".join(t for _, t in fallback_pages))

        file_rows = matches_by_file.get(fname, [])
        all_found = True
        for term in terms:
            term_rows = [r for r in file_rows if (r.get("term") or terms[0]) == term]
            if term in found_terms and term_rows:
                existing_pages = {r["page_number"] for r in term_rows if r["page_number"]}
                for p in pages_by_term[term]:
                    if str(p) not in existing_pages:
                        extra_page_rows.append(_complete_row({
                            "filename": fname, "document_identifier": term_rows[0]["document_identifier"],
                            "collection": row["collection"], "congress": row["congress"], "page_number": p,
                            "matching_text_context": VERIFY_CONTEXT, "api_endpoint": VERIFY_ENDPOINT,
                            "term": term,
                        }, row))
            elif term in found_terms:
                all_found = False
                discrepancies.append({"filename": fname, "term": term,
                                      "issue": "term found in local PDF extraction but no matches.csv row exists"})
            else:
                all_found = False
                discrepancies.append({"filename": fname, "term": term,
                                      "issue": "term NOT found via local PDF re-extraction despite API-driven match "
                                               "(likely image/OCR-only PDF or extraction limitation)"})
        confirmed += int(all_found)

    if extra_page_rows:
        core.upsert_matches(extra_page_rows)
        insert_match_rows(extra_page_rows)

    return {"checked": len(pdf_rows), "confirmed": confirmed, "discrepancies": discrepancies}


# ============================================================================
# Archive validation
# ============================================================================

def validate_archive():
    metadata_path = core.ARCHIVE_DIR / "metadata.csv"
    matches_path = core.ARCHIVE_DIR / "matches.csv"
    metadata_rows = []
    match_rows = []
    problems = []

    if metadata_path.exists():
        with metadata_path.open(newline="", encoding="utf-8-sig") as stream:
            metadata_rows = list(csv.DictReader(stream))
    else:
        problems.append("metadata.csv is missing")
    if matches_path.exists():
        with matches_path.open(newline="", encoding="utf-8-sig") as stream:
            match_rows = list(csv.DictReader(stream))
    else:
        problems.append("matches.csv is missing")

    matched = set()  # (filename or sha256, term) pairs that have a match record
    for row in match_rows:
        term = row.get("term") or core.SEARCH.terms[0]
        matched.add((row.get("filename"), term))
        matched.add((row.get("pdf_sha256"), term))
    expected = {}  # PDF path -> metadata row
    for row in metadata_rows:
        name = row.get("filename", "")
        for term in matched_terms(row):
            expected[core.term_pdf_dir(term) / name] = row
            if (name, term) not in matched and ((row.get("pdf_sha256") or row.get("sha256")), term) not in matched:
                problems.append(f"{name}: missing match record for term {term!r}")
        sidecar = row.get("sidecar_path")
        if not sidecar or not (core.ARCHIVE_DIR / sidecar).is_file():
            problems.append(f"{name}: missing metadata sidecar")

    on_disk = set()
    for pdf_dir in core.all_pdf_dirs():
        if pdf_dir.exists():
            on_disk.update(pdf_dir.glob("*.pdf"))
    for path in sorted(on_disk - set(expected)):
        problems.append(f"{path.relative_to(core.ARCHIVE_DIR)}: PDF has no metadata row for this term folder")
    checked = 0
    for path, row in sorted(expected.items()):
        rel = path.relative_to(core.ARCHIVE_DIR)
        if not path.exists():
            problems.append(f"{rel}: PDF listed in metadata.csv is missing")
            continue
        checked += 1
        if hashlib.sha256(path.read_bytes()).hexdigest() != (row.get("pdf_sha256") or row.get("sha256")):
            problems.append(f"{rel}: SHA-256 differs from metadata")

    lines = [
        f"Congress.gov Term Search Validation: {core.SEARCH.name}",
        f"Terms: {', '.join(core.SEARCH.terms)}",
        f"PDF files checked: {checked}",
        f"Metadata rows: {len(metadata_rows)}",
        f"Match rows: {len(match_rows)}",
        f"Gaps found: {len(problems)}",
        f"Result: {'PASS' if not problems else 'FAIL'}",
    ]
    if problems:
        lines.append("Gaps:")
        lines.extend(f"- {problem}" for problem in problems)
    else:
        lines.append("Every PDF has a metadata row, a matching SHA-256, a sidecar, and a match record.")
    report = "\n".join(lines) + "\n"
    (core.ARCHIVE_DIR / "validation_report.txt").write_text(report, encoding="utf-8")
    print(report, end="")
    return {"pdf_count": checked, "metadata_rows": len(metadata_rows),
            "match_rows": len(match_rows), "problems": problems, "passed": not problems}


# ============================================================================
# Reports: per-Congress counters, per-term views, README
# ============================================================================

def write_term_views():
    """For a multi-term search, write each term folder's own metadata.csv and
    matches.csv (the rows of the search-wide CSVs that belong to that term).
    The search-wide CSVs and SQLite remain the complete record."""
    spec = core.SEARCH
    if not spec.multi_term:
        return
    archive = core.ARCHIVE_DIR
    with (archive / "metadata.csv").open(newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        meta_fields, metadata = reader.fieldnames or [], list(reader)
    with (archive / "matches.csv").open(newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        match_fields, matches = reader.fieldnames or [], list(reader)
    for term in spec.terms:
        folder = core.term_dir(term)
        folder.mkdir(parents=True, exist_ok=True)
        for name, fields, rows in (
            ("metadata.csv", meta_fields, [r for r in metadata if term in matched_terms(r)]),
            ("matches.csv", match_fields, [r for r in matches if (r.get("term") or spec.terms[0]) == term]),
        ):
            with (folder / name).open("w", newline="", encoding="utf-8") as stream:
                writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(rows)
        (folder / "TERM.txt").write_text(
            f"Term: {term}\nSearch: {spec.name} ({spec.describe()})\n"
            "These CSVs are the rows of the search-wide metadata.csv/matches.csv for this term.\n",
            encoding="utf-8")


def persist_collection_counters(counters):
    """counters maps (collection, congress) -> stats. Writes per-Congress rows to
    collection_congress_stats and all-Congress totals to collection_stats."""
    db_path = core.ARCHIVE_DIR / "archive.sqlite"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    totals = {}
    with sqlite3.connect(db_path) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS collection_stats (
            collection TEXT PRIMARY KEY, records_examined INTEGER NOT NULL,
            full_text_searched INTEGER NOT NULL, matches INTEGER NOT NULL,
            unique_pdfs INTEGER NOT NULL
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS collection_congress_stats (
            collection TEXT NOT NULL, congress INTEGER NOT NULL, records_examined INTEGER NOT NULL,
            full_text_searched INTEGER NOT NULL, matches INTEGER NOT NULL,
            unique_pdfs INTEGER NOT NULL, PRIMARY KEY(collection, congress)
        )""")
        for (collection, congress), stats in counters.items():
            db.execute("""INSERT INTO collection_congress_stats VALUES(?,?,?,?,?,?)
                ON CONFLICT(collection, congress) DO UPDATE SET records_examined=excluded.records_examined,
                full_text_searched=excluded.full_text_searched, matches=excluded.matches,
                unique_pdfs=excluded.unique_pdfs""",
                (collection, int(congress), int(stats.get("examined", 0)),
                 int(bool(stats.get("full_text_searched"))), int(stats.get("matches", 0)),
                 len(stats.get("unique_pdfs", set()))))
            total = totals.setdefault(collection, {"examined": 0, "full_text_searched": False,
                                                   "matches": 0, "unique_pdfs": set()})
            total["examined"] += stats.get("examined", 0)
            total["full_text_searched"] = total["full_text_searched"] or bool(stats.get("full_text_searched"))
            total["matches"] += stats.get("matches", 0)
            total["unique_pdfs"] |= set(stats.get("unique_pdfs", set()))
        for collection, stats in totals.items():
            db.execute("""INSERT INTO collection_stats VALUES(?,?,?,?,?)
                ON CONFLICT(collection) DO UPDATE SET records_examined=excluded.records_examined,
                full_text_searched=excluded.full_text_searched, matches=excluded.matches,
                unique_pdfs=excluded.unique_pdfs""",
                (collection, stats["examined"], int(stats["full_text_searched"]),
                 stats["matches"], len(stats["unique_pdfs"])))


def write_readme(congresses, collections, start_time, end_time,
                 pdf_verification_ran, pdf_verification, validation, failures=None):
    archive = core.ARCHIVE_DIR
    metadata_path = archive / "metadata.csv"
    matches_path = archive / "matches.csv"
    metadata = []
    matches = []
    if metadata_path.exists():
        with metadata_path.open(newline="", encoding="utf-8-sig") as stream:
            metadata = list(csv.DictReader(stream))
    if matches_path.exists():
        with matches_path.open(newline="", encoding="utf-8-sig") as stream:
            matches = list(csv.DictReader(stream))
    congress_by_file = {row.get("filename"): row.get("congress", "") for row in metadata}
    metadata_by_key = Counter((row.get("congress", ""), row.get("collection", "")) for row in metadata)
    match_by_key = Counter((row.get("congress") or congress_by_file.get(row.get("filename"), ""),
                            row.get("collection", "")) for row in matches)

    spec = core.SEARCH
    lines = [
        f"Congress.gov Term Search: {spec.name}",
        "=" * 52,
        f"Run started: {start_time.isoformat()}",
        f"Run finished: {end_time.isoformat()}",
        f"Congress scope (this run): {', '.join(map(str, congresses))}",
        f"Matching: {spec.describe()}",
        "Terms:",
        *[f"  {term}" + (f"  -> terms/{spec.folders[term]}/" if spec.multi_term else "") for term in spec.terms],
        "",
        "Persisted archive facts:",
        f"  Metadata rows (unique PDF rows): {len(metadata)}",
        f"  Match occurrence rows: {len(matches)}",
        f"  SQLite database: {archive / 'archive.sqlite'}",
    ]
    db_path = archive / "archive.sqlite"
    if db_path.exists():
        with sqlite3.connect(db_path) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "documents" in tables:
                lines.append(f"  SQLite documents: {db.execute('SELECT COUNT(*) FROM documents').fetchone()[0]}")
            if "files" in tables:
                lines.append(f"  SQLite file links: {db.execute('SELECT COUNT(*) FROM files').fetchone()[0]}")
            if "matches" in tables:
                lines.append(f"  SQLite matches: {db.execute('SELECT COUNT(*) FROM matches').fetchone()[0]}")
            if "collection_congress_stats" in tables:
                lines.append("")
                lines.append("Crawl counters by Congress (every Congress crawled into this archive):")
                lines.append("Congress | Collection | Records examined | Full text searched | Occurrences | Unique PDFs")
                for row in db.execute("SELECT congress, collection, records_examined, full_text_searched, matches, "
                                      "unique_pdfs FROM collection_congress_stats ORDER BY congress, collection"):
                    lines.append(" | ".join(map(str, (row[0], row[1], row[2], bool(row[3]), row[4], row[5]))))
            if "collection_stats" in tables:
                lines.append("")
                lines.append("Crawl counters, totals across Congresses:")
                lines.append("Collection | Records examined | Full text searched | Occurrences | Unique PDFs")
                for row in db.execute("SELECT collection, records_examined, full_text_searched, matches, unique_pdfs FROM collection_stats ORDER BY collection"):
                    lines.append(" | ".join(map(str, (row[0], row[1], bool(row[2]), row[3], row[4]))))
    lines.extend([
        "",
        "CSV totals (CRS rows have a blank Congress; the CRS API has no Congress field):",
        "Congress | Collection | Metadata rows | Match occurrences",
    ])
    for key in sorted(set(metadata_by_key) | set(match_by_key), key=lambda k: (str(k[0]).zfill(4), k[1])):
        lines.append(f"{key[0]} | {key[1]} | {metadata_by_key[key]} | {match_by_key[key]}")
    lines.extend(["", "By term:", "Term | PDFs | Match occurrences"])
    occurrences_by_term = Counter(row.get("term") or spec.terms[0] for row in matches)
    for term in spec.terms:
        pdfs = len(list(core.term_pdf_dir(term).glob("*.pdf")))
        lines.append(f"{term} | {pdfs} | {occurrences_by_term[term]}")
    lines.extend([
        "",
        "Verification status:",
    ])
    lines.append(f"  Local PDF text verification: {'ran' if pdf_verification_ran else 'skipped'}")
    if pdf_verification_ran:
        lines.append(f"  PDFs checked: {pdf_verification.get('checked', 0)}; confirmed: {pdf_verification.get('confirmed', 0)}; discrepancies: {len(pdf_verification.get('discrepancies', []))}")
        for d in pdf_verification.get("discrepancies", []):
            lines.append(f"    {d['filename']}" + (f" [{d['term']}]" if d.get("term") else "") + f": {d['issue']}")
    lines.extend([
        f"  Archive validation: {'PASS' if validation.get('passed') else 'FAIL'}",
        f"  Validation report: {archive / 'validation_report.txt'}",
        "",
        f"Matched documents not marked processed: {len(failures or [])}",
    ])
    for failure in failures or []:
        lines.append(f"  {failure.get('collection')} | {failure.get('document_identifier')} | {failure.get('error') or 'interrupted before completion'}")
    lines.extend([
        "",
        "Scope and known limitations:",
        "  CRS products have no Congress field; the crawler uses the configured publish-date range as its scope basis.",
        "  CRS products use the Congress.gov /crsreport/{id} endpoint and currentVersion metadata.",
        "  The Bound Congressional Record API exposes only page-range metadata (no text or PDF links), so it is probed but not searched.",
        "  Missing optional API values remain null; empty API lists remain empty lists.",
        "  Search coverage is limited to the configured Congresses and collections exposed by the implemented API crawlers.",
        "  A document counts as examined only where the API exposes its text; older Congresses expose less.",
    ])
    (archive / "README.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
