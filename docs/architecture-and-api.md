# Server architecture and API

The composition root is [create_app()](../server/app/main.py). It creates the database/session
factory, loads the shared template, registers request limits and compatibility policy, bootstraps an
empty installation when requested, and installs authenticated routes. Development can use SQLite;
deployment uses PostgreSQL.

## Source ownership

| Module | Responsibility |
| --- | --- |
| [main.py](../server/app/main.py) | HTTP inputs, access checks, report transactions, media routes and exports |
| [models.py](../server/app/models.py) | Tables, SQLite/PostgreSQL session creation and explicit station/index upgrades |
| [security.py](../server/app/security.py), [compatibility.py](../server/app/compatibility.py) | Credential/token primitives, login buckets and admitted client-version interval |
| [checklist.py](../server/app/checklist.py), [measurements.py](../server/app/measurements.py), [report_extras.py](../server/app/report_extras.py) | Connector expansion, editable-v6 corrections, reading and signature/copy validation |
| [stations.py](../server/app/stations.py) | Shared physical station identity, immutable PCVE IDs, verified fields and closure checks |
| [importer.py](../server/app/importer.py), [import_profiles.py](../server/app/import_profiles.py) | Bounded Excel parsing, mapping and built-in Stations layout |
| [images.py](../server/app/images.py), [media.py](../server/app/media.py) | Actual image/video inspection rather than trusting filename/MIME |
| [pdf_report.py](../server/app/pdf_report.py), [exports.py](../server/app/exports.py) | Snapshot-based PDF rendering and bounded PDF/JSON/media ZIP output |
| [contract_trash.py](../server/app/contract_trash.py) | Contract retention and permanent cleanup |
| [admin_cli.py](../server/app/admin_cli.py) | Local administrator commands and interactive console |

## Persistent model

A `Station` is the physical charger identity shared across contracts. A `Charger` is that station's
membership in one contract. `Assignment` grants technician access to a contract. `Report` stores the
intervention owner, revision, dates, payload, checklist and charger/contract snapshot. `Photo`
stores evidence bytes and metadata; despite its name, it includes supported videos.

`SessionToken` stores a token digest/expiry. `OwnershipTransfer` and `Audit` retain administrative
history. `DeletedReport` is a tombstone, allowing an old client cache to distinguish permanent
deletion from a report that never existed. `ContractTrash` retains a contract and its dependent data
until restoration or purge. `ImportBatch` makes import retry identity explicit; `ImportProfile`
stores reusable company mapping.

`database()` creates missing tables and performs the explicit station/index upgrades in this source.
It is not a general column-migration framework. Preserve data and configuration when updating; do
not use a reset to bypass an unexplained schema error.

## Authentication and permissions

Except health and compatibility discovery, API requests require `X-Lem-Client-Version`; most also
require `Authorization: Bearer <token>`. Client versions outside the configured **minimum-to-latest
inclusive interval**, including missing/malformed/newer versions, receive 426. A token alone does
not establish protocol compatibility.

New logins receive a bearer token with a fixed 365-day lifetime from issuance in UTC. The server
checks its expiry on every authenticated request and returns 401 for expired or revoked tokens.
Existing tokens retain their original expiry. The [administration console](administration.md)
can revoke one account's sessions or all sessions, including sessions saved by client apps.

Administrators manage contracts, assignments and ownership. Technicians operate within assigned
contracts and edit their own drafts. A technician cannot delete another owner's work or a closed
report. Administrators have wider deletion/ownership actions, but database revision and context
checks still apply. Console-only system/deleted identities are not login accounts.

The dependency/transaction layer rechecks ownership and revision when committing, not only when
initially reading a report. Read/download paths also enforce contract access. See
[security](../../Common/docs/security.md) for implementation limits and deployment boundaries.

## Endpoint map

`{cid}`, `{rid}`, `{pid}` and `{user_id}` represent IDs, not literal path segments. Read the typed
inputs in `main.py` for exact field validation. Interactive `/docs`, `/redoc` and `/openapi.json`
are enabled only when `LEM_API_DOCS=1`.

| Methods and paths | Purpose |
| --- | --- |
| GET `/api/health`, `/api/client-compatibility` | Public service/version-policy checks |
| POST `/api/login`, `/api/logout` | Start/revoke a session |
| GET `/api/template` | Initial shared checklist |
| GET/POST `/api/users` | Administrator account listing/creation |
| PUT `/api/account/password` | Authenticated technician or administrator changes their own password with `current_password`, `new_password` and `confirmation`. Keeps the calling session and revokes the account’s other sessions. |
| PUT `/api/users/{user_id}/password` | Administrator resets a technician's password and revokes all their sessions |
| GET/POST `/api/contracts` | Visible contracts / administrator creation |
| PUT `/api/contracts/{cid}/presentation` | Administrator issuer/client-logo update with `base_revision` |
| GET `/api/contracts/{cid}/presentation` | Current authorized contract presentation before online export |
| PUT `/api/contracts/{cid}/users/{user_id}` | Assign access |
| GET `/api/admin/deleted-contracts`; POST `/api/contracts/{cid}/trash`, `/restore` | Contract trash workflow |
| GET `/api/contracts/{cid}/chargers` | Contract memberships with the current central station details and report summaries |
| GET `/api/contracts/{cid}/removed-chargers`; POST `/api/contracts/{cid}/chargers/{charger_id}/restore` | Administrator lists recoverable preventive IDs and their `purge_after`, then restores the association and its reports. Reimport within 14 days has the same effect. |
| GET `/api/corrective-contracts/{cid}/members`; DELETE `/api/corrective-contracts/{cid}/members/{sid}`; POST `/api/corrective-contracts/{cid}/members/{sid}/restore` | Administrator lists active and recoverable station IDs, removes coverage for 14 days or restores it together with the interventions removed by that operation. |
| POST `/api/interventions/{cid}/trash`; POST `/api/contracts/{cid}/restore` | Administrator deletes a pending intervention with `{"confirmation":"APAGAR"}` or restores it within 48 hours. GET `/api/admin/deleted-contracts` identifies recoverable requests by `kind` and `purge_after`. A closed report blocks this deletion (409). |
| GET / PUT `/api/contracts/{cid}/chargers/{charger_id}/notes` | Read current membership notes, or save them as an administrator using `notes` and `base_revision`. Android and desktop use the same permissions. |
| GET `/api/stations` | One record per PCVE across all active assigned contracts for technicians, or the entire catalogue for administrators |
| GET `/api/stations/{sid}/reports` | Readable report summaries for a visible PCVE, including reports granted through individual work-order visits. IDs currently held for 14-day recovery are excluded. Trash and correction visibility follow report access rules. |
| DELETE `/api/contracts/{cid}/chargers/{charger_id}` | Administrator removes the preventive ID for 14 days. Repeating removal preserves the deadline. During this period its reports are hidden |
| POST `/api/stations/import`; PATCH `/api/stations/{sid}` | Desktop administrator Excel preview and atomic import, or revision-checked edit |
| GET `/api/companies/{company}/import-profile` | Saved mapping |
| POST `/api/imports/analyse`, `/api/contracts/{cid}/import` | Excel analysis, preview and atomic import |
| GET `/api/reports/{rid}/exists`, `/api/reports/{rid}` | Creation retry probe / full report |
| PUT `/api/reports/{rid}` | Revision-checked create/update/close |
| GET `/api/reports/{rid}/owners`; POST `/api/reports/{rid}/owner` | Ownership candidates and transfer |
| DELETE `/api/reports/{rid}` | Permanent deletion with revision and draft evidence preconditions |
| POST `/api/reports/{rid}/corrections` | Desktop administrator begins an independent working copy (`id`, `base_revision`) |
| POST `/api/reports/{rid}/correction/commit` | Validate and atomically replace the closed source using a full ReportInput |
| PUT/GET/DELETE `/api/reports/{rid}/photos/{pid}` | Evidence bytes and removals |
| GET `/api/reports/{rid}/pdf`, `/api/reports/{rid}/bundle` | Export an eligible closed report |

## Station catalogue

A station is keyed by its unique spreadsheet ID. Contract membership retains its own UUID for
reports and permissions. The station list returns the physical UUID, external ID, details and
`updated` revision. Contract rows expose that revision as `station_updated`. The station PATCH
accepts `base_updated` and a map of changed fields. The server returns 409 when a conflicting edit
has advanced the revision.

Station and contract imports run the same Excel parser and merge into the same station records.
The parser extracts **Notas** before constructing station details and raw source fields. Contract
import stores it in `contract_charger_notes`, keyed by the membership UUID. Contract listings
return `contract_notes` and `notes_revision`. A missing note has revision zero. A changed text,
including clearing a note, advances its revision. An identical retry succeeds and a stale edit
of different text returns 409. Existing reports can read notes after detachment. Editing resumes
when the ID is added back to the contract. Permanent contract deletion cascades to its notes.

Station imports can create records before assignment to a contract. Contract imports also create
membership links. Preview is read-only. Previously verified identity fields survive workbook
imports and can be changed explicitly through the station editor. Closing a report can update the
station details while preserving the snapshots of earlier reports.

## Report write lifecycle

A report PUT includes `checklist_version`, `charger_id`, `base_revision`, `status` and `payload`.
On first upload the app sends `station_identity_at_start` with the manufacturer, model and serial
from the device's opening snapshot. This reference stays frozen over retries. Closure changes the
shared station only for a technician correction that passes the concurrent-value check.
For older clients the server treats identity answers already present on first upload as observations.
Later edits are compared with those values. Connector metadata may also be supplied for new work.
Report responses include the physical `station_id` even when the historical snapshot predates the catalogue.
A new report begins at base revision zero.
Existing reports must match their saved checklist version, charger and known revision.

Only the current owner may update answers/evidence or close a report, including when the caller
is an administrator. Report writes lock the row before validating closure, serializing against
evidence changes and ownership transfers. Catalogue summaries include the report revision so
clients can distinguish server changes from stale cached drafts.

Draft writes accept incomplete answers while still validating shapes, identifiers and provided
signature/copy metadata. Closure additionally checks required values, separate assessments,
observations, actual photographs and referenced signature evidence. Blank optional notes entries are
allowed. [The report contract](../../Common/docs/report-contract.md) defines the current rules.

New reports use the current template. Existing editable v6 reports receive the targeted v6
corrections; closed reports retain their saved template. This is a deliberate exception to treating
every draft template as permanently frozen.

Evidence uploads include `checkpoint` and `base_revision`. Retrying an existing evidence ID
succeeds only for matching report/checkpoint/digest data; changed bytes under the same ID are
rejected. Photo DELETE requires a matching `base_revision` even if the evidence is already absent.
A matching-base no-op returns the same revision. A real removal advances it exactly once. A stale
request returns HTTP 409. After a lost response, clients can read the full report to verify the
expected next revision, unchanged payload and absent evidence before continuing. Repeating the
DELETE at the verified revision is a no-op. Evidence is accepted before the final closure request,
so a closed local report is normally sent as draft → evidence → closed.

## Export and background work

PDF/ZIP rendering uses the saved report answers/identity and the contract's current presentation.
`presentation` contains `issuer` (`Lem`/`CSMTEC`), `client_logos` (0–2 artwork entries),
and its own `revision`; it appears in contract and full-report responses. Settings writes require
administrator access and the contract write lock. Stale `base_revision` returns 409. Exports hold
the contract read lock on PostgreSQL only while staging their snapshot. Atomic revision updates and a single joined
presentation read also prevent lost edits or mixed issuer/client logos on SQLite.
Required files must exist;
missing evidence is reported rather than silently omitted. Both exports stage their metadata
and required originals under the source locks, then release the DB session before rendering
the PDF or assembling the ZIP. The PDF is returned as bytes and the ZIP streams from a spool.
The PDF renders staged photographs and lists videos from their metadata. A ZIP stages
each original once, reuses the staged photographs for its PDF and includes the original videos.
An accepted export can therefore finish with its original content/presentation even if an
administrator commits a correction or changes branding during rendering. A detached export never
reopens the database to recover a missing temporary file: it fails and the user can retry.
Staging/rendering failures clean up temporary files and leave the source/correction records intact.
Already-compressed media and PDF entries are stored without ZIP recompression. Export/image/download
admission slots fail fast with 503 instead of holding database connections while waiting.
Evidence upload admission runs before multipart body consumption. Busy uploads return HTTP 503
with `Retry-After: 15`. The admitted slot also bounds temporary multipart storage. Video acceptance
checks complete MP4/3GP boxes and reads referenced packets using the bounded FFprobe process.

Original evidence GET supports single byte ranges (206), SHA-256 ETags and `If-Range`; an invalid
range returns 416 with the representation length. Access and report/photo association are checked
before serving any bytes. A private temporary disk cache shares immutable originals between devices
and range requests, with one loader per original and two database BLOB loads at most in parallel.
The database closes before cache waits or sending. Responses track independent byte positions and read
256-KiB blocks. Disconnects and writes stalled for 30 seconds release the lease and admission slot.
The idle cache budget is 512 MiB and 2048 files, with ten-minute expiry checked on access. Private
temporary files are released by the operating system if the worker exits. Download overload returns
503 with `Retry-After: 2`. The client staggers retries for busy original downloads.

`report_corrections` records source revision, editor, original owner, cloned photo mapping, opening
station identity and commit fingerprint. Editing copies require admin plus
`X-Lem-Client-Platform: windows|linux`, and only their editor can access them. This header implements
a supported-platform policy, not hardware attestation.
Catalogue projections hide working copies from other users/platforms. Normal report PUT/photo gates
also apply to correction drafts; only the commit endpoint can replace the source. Ordinary report
ownership rules remain unchanged. Deletion with `correction_only=true` cannot remove a final report.
The server console can cancel an orphaned working copy but cannot transfer it or delete its source
while it is active. See the [shared correction contract](../../Common/docs/report-contract.md).

Committing changed EST.02/03/04 answers updates the physical station and replacement snapshot
atomically. The private `identity_baseline` column captures the current model, manufacturer and
serial when the correction opens. A conflicting edit after that point returns 409 with reason
`station_identity_conflict` and preserves the original, correction and catalogue. The nullable
column is added during database startup. For an existing open copy, the server can use live
identity as its baseline when the catalogue write is older than the copy. Otherwise it checks
against the historical identity to protect newer data.
Committed correction responses include live `station_update` data on POST and GET for client cache
refresh and lost-response recovery. Retrying the same commit never reapplies its station edits.
Correction commit takes the station-catalogue lock before charger/report locks, matching ordinary
closure/import. Reversing that order creates a PostgreSQL deadlock when both operations overlap.
Every cloned signature reference, including `legacy_signatures` on a repeated correction, follows
the new evidence IDs; it must not refer to the source deleted after a successful replacement.

Report responses and catalogue summaries expose a public `identifier` in the format
`ID_Contrato`, derived from the charger and contract snapshot. Catalogue summaries supply
start and finish separately. PDF and ZIP download headers use the identifier with UTF-8
filename support. The archive contains the equally named PDF and JSON and groups original
media by checkpoint. Media filenames share the identifier and add the evidence UUID.
UUIDs address reports and evidence in the API and storage.
[Naming rules](../../Common/docs/report-contract.md#public-report-identifiers-and-export-names)

The lifespan task checks expired contract trash and removed IDs roughly every minute. The 48-hour and 14-day limits use UTC and exclude the exact deadline. Corrective coverage restore and expiry take the same intervention locks and check the deadline after acquiring them. Permanently purging a pending intervention resolves its work-order visit in the same transaction. A job already deleted separately is not restored with its coverage. Restore coverage before separately restoring the job. Export/media limits and retry
semantics are documented in [security](../../Common/docs/security.md); operator procedures are in
[administration](administration.md).

The preventive charger and active corrective member listings include `membership_revision`.
Send the observed value as a query parameter on membership DELETE, including on retries. A retry
at the same revision preserves the deadline. Recovery or reattachment advances the revision and
an older DELETE receives 409. Missing revisions are treated as zero for original memberships.

### Checklist v8 migration and compatibility

App/API 0.11.2 use v8. The server checks client compatibility on login and authenticated requests. Draft reads project v6 through v7 to v8 and v7 directly to v8. The next write persists the projection and moves the evidence checkpoints atomically with the revision check. Creating a correction upgrades its working copy and preserves the closed original. The server recalculates MAN.13 (mA) and MAN.20 (Ω) using MAN.11. MAN.12 and MAN.14 store integer milliseconds or NA. MAN.14 also records the technician’s result. See the [report contract](../../Common/docs/report-contract.md).
Sync can request `include_stored_payload=true` on a report GET. If the visible form has been
projected, `stored_payload` provides the original answer snapshot for a lost evidence acknowledgement.
Independent photo uploads also persist that migration before validating the projected checkpoint.
Invalid media and stale revisions leave the saved legacy report and originals intact.
Pre-v6 correction copies use the current form with an optional `ANT` section preserving old
answers, observations and evidence. Reused IDs are never treated as current station-identity
fields. Their historical signatures remain evidence; current questions require a new signature.
GET projects an already-open old correction identically to its transactional write migration.
`frozen_history: true` permits completing the staged upload of a previously closed offline v6 or v7 report;
the temporary `template.frozen_upload` state disables editing until its original closure completes.

## Work orders

Administrators can combine preventive memberships, corrective jobs and commissioning jobs in one
ordered list. `GET /api/work-order-candidates` returns targets without a closed report or a pending
order item. `PUT /api/work-orders/{uuid}` takes the same client-generated id, revision, ISO start/due
dates, technician UUIDs and ordered target UUIDs. Each intervention target addresses its own visit,
so one PCVE may have several independently actionable rows. Corrective and commissioning items carry
their public reference in the candidate and order responses. On commissioning rows, `contract` is
null and `contract_id` identifies the internal report scope. Empty orders and repeated targets are
rejected. The code uses the original start month and a serialized monthly counter. A resolved item
retains its result, actor and time.

### Corrective coverage and station interventions

`PUT /api/corrective-contracts/{uuid}` accepts multipart fields `company`, `name`, `start`, `end` and an XLSX `file`. The server reads existing external PCVE IDs from column A of the first worksheet, starting at row 1. It validates all rows before committing coverage. The contract's public name uses its start year, customer and name separated by underscores. The interval may begin on or after 1 January of the current Lisbon year and end up to two calendar years later. Both dates are inclusive. For a leap-day start the anniversary falls on 28 February in a non-leap year.

`GET /api/corrective-contracts?station_id={uuid}` lists the administrator's eligible choices for today, latest start first, then name. `PUT /api/interventions/{uuid}` takes a station UUID, a `kind` of `corrective` or `commissioning`, and optional `technician` and `notes`. For a corrective, omitting `contract_id` applies the first eligible contract. Explicit null creates sporadic work. A supplied contract must cover this station today. Commissioning requires an empty contract choice. Technicians can commission their own visible stations. Administrators can create either type and share a job via `PUT /api/interventions/{uuid}/share`, with the previous and desired technician UUIDs or null. Creation retries use the original request UUID.

The report service represents each intervention by a single-member access scope. Its scope and report target share the intervention UUID. Clients include `X-Lem-Work-Orders: 1` on `GET /api/contracts` to receive intervention scopes. Each such row has an `intervention` object and appears in the intervention menus. The default catalogue lists ordinary contracts. The optional commercial contract is stored independently. Existing membership notes, scoped work-order grants, evidence, closure and corrections apply to the intervention's report. The database catalogue lock serializes creation, planning and the first report claim. For both types, the order grants the assigned crew access to the visit. New reports and closure must wait until the order starts, including closure of an existing directly shared draft.

The early-write response is HTTP 409 with `reason = "work_order_not_started"` and the existing Portuguese message in `detail`. The client can recheck this condition using the same report UUID. A resolved visit's draft remains bound to that visit. The active-report guard and intervention catalogue consider the current draft or closed report for the job.

When an individually assigned technician owns the current draft, the administrator's intervention catalogue includes `handoff` with `report_id` and `base_revision`. Send it with `previous` and the new `technician` to `PUT /api/interventions/{uuid}/share`. An accepted handoff changes the owner and individual grant in one transaction, advances the report revision, and records the ownership change. A stale request preserves both the assignment and the draft.

`GET /api/work-orders` lists all orders for administrators and assigned orders for technicians. The header `X-Lem-Work-Orders: 1` includes limited contracts in the ordinary catalogue. The server enforces each membership and Lisbon start date on report creation and updates. An independent whole-contract grant remains effective. Overdue items remain actionable. Closing a report resolves its pending membership in the same transaction. Invalidating via `PUT /api/work-orders/{id}/items/{item}/invalid` requires an assigned technician, revision and observation. It leaves the contract eligible for a future visit.

`GET /api/work-orders?include_cancelled=true` returns a cancelled order, its cancellation time, actor and original item sequence for 24 elapsed hours after cancellation. An administrator can `PUT /api/work-orders/{id}/cancel` with the current revision before the Lisbon start day. This releases pending memberships and reminders. Repeating a successful cancellation during those 24 hours returns the same time and revision.

At the UTC deadline the order disappears from the API. The background cleanup runs on startup and every minute, deleting the order, items, assignments and notifications. It retains the allocated UUID and code to prevent request replays and number reuse. Reports and attachments survive. A draft bound to a deleted visit retains its original work item UUID and is returned with `work_item_expired: true` for read-only recovery.

Limited contracts include `order_targets` so clients can retain authorised offline rows when another contract refreshes. Planning reserves one ordinary draft per pending membership. Draft summaries expose owner, report revision and attachment count. At resolution, any other existing drafts are recorded for read-only recovery by their still-assigned owners. A removed closed report retains its identifier and completion record with `report_removed` set.

During 14-day preventive recovery the catalogue excludes the ID and its server-side reports, including from work orders, station history and export. Restoring the ID reopens normal access. Legacy detachments without a recovery deadline keep the previous draft visibility.
An exact report grant remains valid for an advertised resolved report after the pending visit's scope
leaves the contract catalogue. A closed report GET returns the current versioned `station_update`
alongside the immutable report snapshot so a lost closure response can be reconciled safely.

Draft deletion compares `base_revision` and the evidence list under the report lock shared with
uploads. Send `evidence_digest` as the SHA-256 of the UTF-8 lines `<photo-id> <digest>\n`,
sorted by photo ID. The API returns 409 on a mismatch or a missing digest for a draft with evidence.
An empty list hashes to the SHA-256 of an empty string. Retrying a completed delete returns its
tombstone acknowledgement.

The creator follows automatically. Other administrators subscribe through `PUT /api/work-orders/{id}/follow`. A follower can unsubscribe after completion. Each installation reads its signed-in user's inbox at `GET /api/work-order-notices/{device}` and acknowledges individual notifications with a PUT to the appended notice UUID. Start notices are returned during the Lisbon start day from 07:00 inclusive to 20:00 exclusive. Old unacknowledged start dates expire. Assignment, impediment and completion events survive offline periods.

### Visit writes and exports in 0.5.4

A pending visit cannot be marked invalid while its contract is in the trash or its PCVE membership is detached. Creating a report against a temporarily detached membership returns HTTP 409 with `reason: "pcve_membership_detached"`. After reattachment, the client can retry the original report and visit subject to the usual ownership and revision checks.

Station edits and standard imports preserve decoded Unicode. Set `repair_legacy_cedillas: true` in an import mapping only for a verified legacy workbook whose text uses Ý or ý in place of cedillas.

Include `work_item_id` on the first and subsequent PUTs from an order. The server stores the original binding in the report transaction. Only the current pending visit can be fulfilled. A prior visit's recovery draft remains available to its eligible owner. Started orders retain their start date. Uploads, closure, membership notes and Excel imports acquire the catalogue lock before contract and membership locks. Each report PUT returns the state it accepted, including the attachment list.

Recovery drafts include `work_item_recovery: true` in report and membership summaries. The owner picker and transfer endpoint evaluate the exact report grant. A scoped technician can receive a recovery draft from their original assigned visit. An independent full grant also gives access. Transfers leave the recovery draft read-only and bound to that same visit.

A contract Excel preview returns an `operation_id`. Send the same ID, workbook and mapping on every retry of its commit. The receipt returns the previous result and preserves intervening note edits. A fresh preview starts an independent import. Both import paths report invalid connector cells during preview and reject the batch at commit. For contract export, authorized users can include legacy detached memberships within their existing report scope. IDs with a 14-day recovery marker stay hidden even with `GET /api/contracts/{id}/chargers?include_detached=true`.
