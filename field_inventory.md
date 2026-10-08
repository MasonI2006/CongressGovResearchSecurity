# Congress.gov Field Inventory

Discovery run: 2026-10-01, 119th Congress. Committee prints were sampled first, followed by one matched document for each other collection with an existing PDF match. The smoke test used the current API cache wherever available; only missing payloads were requested. Raw responses were written once under `raw_api/` with `api_key` parameters removed from URLs. No archive PDFs, CSVs, or processing state were changed; one missing CRS detail response was added to `cache/`.

## Samples and raw responses

| Collection | Sample | Endpoints saved |
|---|---|---|
| Committee prints | `119-house-62529` | `committee-print/119/house/62529`; `/text` |
| Bills | `119-HR-477` | `/bill/119/hr/477`; `/text`, `/cosponsors`, `/subjects`, `/committees`, `/relatedbills` |
| Amendments | `119-SAMDT-2478` | `/amendment/119/samdt/2478`; `/text`, `/actions` |
| Committee reports | `119-HRPT-16` | `/committee-report/119/hrpt/16`; `/text` |
| Hearings | `119-house-58978` | `/hearing/119/house/58978` |
| Committee meetings | `119-house-119563` + matched support document `MTG-119-HOUSE-119563-6-Support_Document` | `/committee-meeting/119/house/119563` |
| Daily Congressional Record | `CR-172-115-Daily_Digest-D714` | `/daily-congressional-record/172/115`; `/articles` |
| CRS product | `CRS-R49457` (detail-shape sample only; not a matched archive PDF) | `/crsreport/R49457` |

No matched CRS PDF existed in the current archive, so the CRS detail sample is only a schema smoke test and was not treated as a phrase match. The Congress.gov API collection is named `crsreport`; `/crsproduct/{id}` is not the endpoint observed in this API. No witness sub-endpoint was called for the hearing because its `associatedMeeting` field was absent. Witness data for the committee-meeting sample was returned directly in its detail payload.

## Common fields

These are archive-computed values, not API fields: `stable_id`, `collection`, `source_system`, `api_base`, `retrieved_at`, `http_status`, `pdf_sha256`, `pdf_bytes`, `pdf_page_count`, `pdf_has_text_layer`, `text_source_used`, and `raw_api_json_path`. In the samples, titles and update dates were present for each selected record. A successful cached/API response represents HTTP 200; status capture still needs to be wired into the downloader. The samples establish actual API metadata shapes only, not computed PDF values.

## Fields observed by collection

### Committee prints

**Present:** `jacketNumber`, `number`, `chamber`, `congress`, `citation`, `title`, `updateDate`, `committees[]` (`name`, `systemCode`, `url`), `associatedBills[]` (`congress`, `number`, `type`, `url`), and a `text` reference. `/text` returned flat `text[]` items with `type` and `url`, including PDF and formatted-text formats.

**Absent:** no requested title/date/stable-ID field was absent in this sample. No separate committee-meeting or witness fields are on this print record.

**Explicit null:** none.

### Bills

**Present:** `congress`, `type`, `number`, `originChamber`, `originChamberCode`, `introducedDate`, `title`, `updateDate`, `updateDateIncludingText`, `latestAction.actionDate`, `latestAction.text`, `policyArea.name`, and `sponsors[]` including `bioguideId`, `fullName`, `party`, and `state`. The detail response also has cosponsor/committee/subject/text-version references. The sub-endpoints returned cosponsor records with `bioguideId` and name/party/state/date fields; committee records with `name` and `systemCode`; `subjects.legislativeSubjects`; and text versions with `type`, `date`, and `formats[].type`/`url`.

**Absent:** `laws`/public-law number was not returned for this sample. The `/relatedbills` response returned an empty related-bills list. `subjects.legislativeSubjects` was present but empty. These are empty/not-returned values, not JSON nulls.

**Explicit null:** none.

### Amendments

**Present:** `congress`, `type`, `number`, `chamber`, `submittedDate`, `updateDate`, `sponsors[]` including `bioguideId`, `fullName`, `party`, and `state`, `amendedBill` with congress/type/number/chamber/title/URL, action and text-version references, and text version `type`/`date`/formats. The `/actions` endpoint returned action entries with `actionDate`, `actionCode`, `type`, and `sourceSystem`.

**Absent:** the detail response had no amendment-level `description`, `purpose`, or `latestAction` object. The observed action entry had no `text` field, so a latest-action text cannot be populated from this sample. `amendedAmendment.purpose` is a separate nested field and does not establish an amendment-level purpose.

**Explicit null:** none.

### Committee reports

**Present:** `citation`, `type`, `number`, `part`, `chamber`, `title`, `issueDate`, `updateDate`, `isConferenceReport`, `sessionNumber`, `reportType`, `committees[]` with `name`/`systemCode`, `associatedBill[]`, and a text reference. `/text` returned nested formats with `type`, `url`, and `isErrata`.

**Absent:** no requested title/date/stable-ID field was absent in this sample. The API field is singular `associatedBill` even though the archive field should be named `associatedBills`.

**Explicit null:** none.

### Hearings

**Present:** `jacketNumber`, `chamber`, `congress`, `title`, `citation`, `dates[]` with `date`, `committees[]` with `name`/`systemCode`, `libraryOfCongressIdentifier`, and `formats[]` with `type`/`url`.

**Absent:** `associatedMeeting` was absent in this sample, so there was no linked event ID to follow. Witnesses are not expected on this endpoint.

**Explicit null:** none.

### Committee meetings

**Present:** `eventId`, `congress`, `chamber`, `title`, `type`, `meetingStatus`, `date`, `updateDate`, `committees[]` with `name`/`systemCode`, `location` with `building`/`room`, `meetingDocuments[]` with `documentType`, `format`, `name`, `description`, and `url`, `witnesses[]` with `name`/`position`/`organization`, and `witnessDocuments[]` with `documentType`/`format`/`url`. The matched PDF is specifically a `Support Document` meeting-document entry.

**Absent:** `relatedItems` was absent; `location.address` was not present in this sample.

**Explicit null:** none.

### Daily Congressional Record

**Present on the issue:** `volumeNumber`, `issueNumber`, `issueDate`, `sessionNumber`, `congress`, `updateDate`, `url`, and `fullIssue` references. **Present on articles:** section `name`; article `title`, `startPage`, `endPage`, and `text[]` format `type`/`url`. The phrase-matched article is in `Daily Digest`, starts at D714, and ends at D716.

**Absent:** article-level `congress` was absent; congress is available from the issue record.

**Explicit null:** none.

### CRS products

**Present on the non-matched detail-shape sample:** `id`, `title`, `contentType`, `publishDate`, `updateDate`, `authors`, `topics`, `summary`, `currentVersion`, `formats`, `relatedMaterials`, and `url`. The API's version key in this response was `currentVersion`, not `version`.

**Absent:** `congress` is not a field on this product response. This product was not matched, so it is not evidence of a responsive CRS PDF.

**Explicit null:** `relatedMaterials[0].title` was JSON null. `topics` was an empty list, not null.

## Discovery caveats

- One existing matched sample was used for each matched collection; availability and nullness can vary by document.
- There were no CRS matches in the existing archive. CRS therefore received a single detail-only shape sample; no CRS sub-endpoint was requested.
- The samples validate the API field names and nesting above. Inline retrieval timestamps, HTTP status capture, SQLite/sidecar writes, and failure-safe processed-state transitions still need implementation before a fresh crawl.
- `api_key` values are not included in these snapshots. This discovery pass did not start the full crawl or wipe `research_security_archive/`, `cache/`, or `state/`.
