"""Shared document processing pipeline: fetch text/PDF, search phrase, save & record."""
from . import client, config, metadata_capture, pdf_utils, store, text_utils

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
                      title, date, congress_url, formats, api_endpoint, force=False):
    """Search a document's available text for the phrase; if found, download the
    PDF, dedupe, and record metadata/match rows. Returns True if a match was found.
    Never raises: any unexpected failure is logged and treated as no-match, so a
    single bad document can't abort a multi-hour crawl.
    """
    try:
        return _process_document_inner(
            doc_id, collection, congress, document_type, document_number,
            title, date, congress_url, formats, api_endpoint, force,
        )
    except Exception as e:
        error = client.redact_text(str(e))
        store.record_processing_failure(doc_id, collection, error)
        print(f"WARN: process_document failed for {error} [document={doc_id}]")
        return False


def _process_document_inner(doc_id, collection, congress, document_type, document_number,
                             title, date, congress_url, formats, api_endpoint, force):
    if not force and store.already_processed(doc_id):
        return False

    formats = formats or []
    non_pdf_fmt = _pick_format(formats, NON_PDF_PRIORITY)
    pdf_fmt = _pick_pdf(formats)

    matches = []  # list of (page_number, context, char_location)
    full_text_searched = False

    if non_pdf_fmt:
        raw = client.fetch_text(non_pdf_fmt["url"])
        if raw is not None:
            full_text_searched = True
            text = text_utils.html_to_text(raw) if _looks_like_markup(raw) else raw
            for m in text_utils.find_matches(text):
                matches.append((None, m["context"], m["start"]))

    pdf_bytes = None
    if pdf_fmt:
        pdf_bytes = client.fetch_binary(pdf_fmt["url"])
        if pdf_bytes and not non_pdf_fmt:
            full_text_searched = True
            pages = pdf_utils.extract_pdf_pages_text(pdf_bytes)
            for page_num, page_text in pages:
                for m in text_utils.find_matches(page_text):
                    matches.append((page_num, m["context"], m["start"]))
            # Cross-page-boundary matches: join consecutive page texts.
            for i in range(len(pages) - 1):
                joined = (pages[i][1][-250:] + " " + pages[i + 1][1][:250])
                for m in text_utils.find_matches(joined):
                    ctx = m["context"]
                    if not any(ctx[:40] in existing_ctx for _, existing_ctx, _ in matches):
                        matches.append((f"{pages[i][0]}-{pages[i+1][0]}", ctx, None))

    if not matches:
        store.note_examined(collection, congress, full_text_searched, doc_id)
        store.mark_processed(doc_id)
        return False

    store.mark_match_pending(doc_id, collection)
    if not pdf_fmt:
        raise RuntimeError(f"Matched document has no PDF format: {doc_id}")
    if pdf_bytes is None:
        pdf_bytes = client.fetch_binary(pdf_fmt["url"])
    if not pdf_bytes:
        raise RuntimeError(f"Matched document PDF could not be downloaded: {doc_id}")

    sha = pdf_utils.sha256_bytes(pdf_bytes)
    candidate_name = store.filename_for_hash(sha, store.safe_filename(doc_id, pdf_fmt["url"]))
    out_path = config.PDF_DIR / candidate_name
    if out_path.exists():
        if pdf_utils.sha256_bytes(out_path.read_bytes()) != sha:
            raise RuntimeError(f"Existing PDF hash differs from downloaded content: {candidate_name}")
    else:
        out_path.write_bytes(pdf_bytes)

    store.note_examined(collection, congress, full_text_searched, doc_id)
    metadata_capture.capture_match(
        collection=collection, document_id=doc_id, congress=congress,
        document_type=document_type, document_number=document_number, title=title,
        document_date=date, congress_url=congress_url, api_endpoint=api_endpoint,
        pdf_url=pdf_fmt["url"], pdf_bytes=pdf_bytes, formats=formats,
        used_text_type=non_pdf_fmt.get("type") if non_pdf_fmt else None,
        matches=matches, filename=candidate_name,
    )
    store.remember_pdf(sha, candidate_name, collection, congress, len(matches))
    store.mark_processed(doc_id)

    return True
