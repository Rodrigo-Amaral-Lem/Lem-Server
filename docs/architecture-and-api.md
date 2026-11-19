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
| [stations.py](../server/app/stations.py) | Shared physical station identity, verified fields and canonical rename/closure checks |
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
| PUT `/api/users/{user_id}/password` | Administrator resets a technician's password and revokes all their sessions |
| GET/POST `/api/contracts` | Visible contracts / administrator creation |
| PUT `/api/contracts/{cid}/presentation` | Administrator issuer/client-logo update with `base_revision` |
| GET `/api/contracts/{cid}/presentation` | Current authorized contract presentation before online export |
| PUT `/api/contracts/{cid}/users/{user_id}` | Assign access |
| GET `/api/admin/deleted-contracts`; POST `/api/contracts/{cid}/trash`, `/restore` | Contract trash workflow |
| GET `/api/contracts/{cid}/chargers` | Contract memberships with the current central station details and report summaries |
| GET `/api/stations` | One record per PCVE across all active assigned contracts for technicians, or the entire catalogue for administrators |
| POST `/api/stations/import`; PATCH `/api/stations/{sid}` | Desktop administrator Excel preview and atomic import, or revision-checked edit |
| GET `/api/companies/{company}/import-profile` | Saved mapping |
| POST `/api/imports/analyse`, `/api/contracts/{cid}/import` | Excel analysis, preview and atomic import |
| GET `/api/reports/{rid}/exists`, `/api/reports/{rid}` | Creation retry probe / full report |
| PUT `/api/reports/{rid}` | Revision-checked create/update/close |
| GET `/api/reports/{rid}/owners`; POST `/api/reports/{rid}/owner` | Ownership candidates and transfer |
| DELETE `/api/reports/{rid}` | Revision-checked permanent report deletion |
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
Station imports can create records before assignment to a contract. Contract imports also create
membership links. Preview is read-only. Previously verified identity fields survive workbook
imports and can be changed explicitly through the station editor. Closing a report can update the
station details while preserving the snapshots of earlier reports.

## Report write lifecycle

A report PUT includes `checklist_version`, `charger_id`, `base_revision`, `status` and `payload`
; connector metadata may also be supplied for new work. A new report begins at base revision zero.
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

Original evidence GET supports single byte ranges (206), SHA-256 ETags and `If-Range`; an invalid
range returns 416 with the representation length. Access and report/photo association are checked
before serving any bytes. A private temporary disk cache shares immutable originals between devices
and range requests, with one loader per original and two database BLOB loads at most in parallel.
The database closes before cache waits or sending. Responses read independent handles in 256-KiB
blocks; disconnect releases the handle and admission slot. Idle cache budget is 512 MiB/2048 files,
with ten-minute expiry checked on access; active leases remain pinned until their responses finish.
Download overload returns 503 with Retry-After 2 seconds. Export admission retains its separate limit.

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

The lifespan task checks expired contract trash roughly every minute. Export/media limits and retry
semantics are documented in [security](../../Common/docs/security.md); operator procedures are in
[administration](administration.md).

### Checklist v7 migration and compatibility

Version 0.2.6 is the minimum client for template v7. Draft reads project legacy v6 data; the next
write persists the template, answer migration and photo checkpoint moves under the same transaction
and revision check. Correction creation upgrades the copy, never the closed original. Validation
recomputes MAN.12/MAN.18 independently from client assessments. See Common/docs/report-contract.md.
Independent photo uploads also persist that migration before validating the projected checkpoint.
Invalid media and stale revisions leave the saved legacy report and originals intact.
Pre-v6 correction copies use the current form with an optional `ANT` section preserving old
answers, observations and evidence. Reused IDs are never treated as current station-identity
fields. Their historical signatures remain evidence; current questions require a new signature.
GET projects an already-open old correction identically to its transactional write migration.
`frozen_history: true` permits completing the staged upload of a previously closed offline v6 report;
the temporary `template.frozen_upload` state disables editing until its original closure completes.
