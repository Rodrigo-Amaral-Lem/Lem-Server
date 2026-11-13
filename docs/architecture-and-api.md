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
| GET/POST `/api/contracts` | Visible contracts / administrator creation |
| PUT `/api/contracts/{cid}/users/{user_id}` | Assign access |
| GET `/api/admin/deleted-contracts`; POST `/api/contracts/{cid}/trash`, `/restore` | Contract trash workflow |
| GET `/api/contracts/{cid}/chargers` | Lightweight catalogue and report summaries |
| GET `/api/companies/{company}/import-profile` | Saved mapping |
| POST `/api/imports/analyse`, `/api/contracts/{cid}/import` | Excel analysis, preview and atomic import |
| GET `/api/reports/{rid}/exists`, `/api/reports/{rid}` | Creation retry probe / full report |
| PUT `/api/reports/{rid}` | Revision-checked create/update/close |
| GET `/api/reports/{rid}/owners`; POST `/api/reports/{rid}/owner` | Ownership candidates and transfer |
| DELETE `/api/reports/{rid}` | Revision-checked permanent report deletion |
| PUT/GET/DELETE `/api/reports/{rid}/photos/{pid}` | Evidence bytes and removals |
| GET `/api/reports/{rid}/pdf`, `/api/reports/{rid}/bundle` | Export an eligible closed report |

## Report write lifecycle

A report PUT includes `checklist_version`, `charger_id`, `base_revision`, `status` and `payload`
; connector metadata may also be supplied for new work. A new report begins at base revision zero.
Existing reports must match their saved checklist version, charger and known revision.

Draft writes accept incomplete answers while still validating shapes, identifiers and provided
signature/copy metadata. Closure additionally checks required values, separate assessments,
observations, actual photographs and referenced signature evidence. Blank optional notes entries are
allowed. [The report contract](../../Common/docs/report-contract.md) defines the current rules.

New reports use the current template. Existing editable v6 reports receive the targeted v6
corrections; closed reports retain their saved template. This is a deliberate exception to treating
every draft template as permanently frozen.

Evidence uploads include `checkpoint` and `base_revision`. Retrying an existing evidence ID
succeeds only for matching report/checkpoint/digest data; changed bytes under the same ID are
rejected. Upload/removal admission rechecks owner and revision. Evidence is accepted before the
final closure request, so a closed local report is normally sent as draft → evidence → closed.

## Export and background work

PDF/ZIP rendering uses the saved report context, not today's catalogue. Required files must exist;
missing evidence is reported rather than silently omitted. PDF is returned as bytes; ZIP output uses
a spooled file and releases the DB session before the slow response stream. Export/image admission
slots fail fast with 503 instead of holding scarce database connections while waiting.

The lifespan task checks expired contract trash roughly every minute. Export/media limits and retry
semantics are documented in [security](../../Common/docs/security.md); operator procedures are in
[administration](administration.md).
