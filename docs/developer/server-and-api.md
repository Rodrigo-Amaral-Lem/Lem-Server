# Server, database and API

## Application/session composition

[run.py](../../server/run.py) calls `create_app()`. Tests supply a temporary database
URL directly. The shared checklist is loaded from `CHECKLIST_FILE` when set, otherwise
from the project `shared/checklist-v1.json`.

`database()` creates the engine, enables SQLite foreign keys when applicable,
creates missing tables and returns a session factory. `db_dep()` yields one session
per request. Mutation handlers explicitly commit; an exception before commit leaves
the operation uncommitted. PostgreSQL supplies the row locks used in deployment;
SQLite tests exercise validation and conditional updates but are not a full substitute
for concurrent PostgreSQL transaction testing.

An empty installation bootstraps an admin from `ADMIN_EMAIL` and `ADMIN_PASSWORD`.
Passwords require at least 12 characters and are encoded as `salt:scrypt_hex`.
Login generates a random token, stores its SHA-256 digest and a 30-day expiry, and
returns the raw token once to the client. HTTPBearer dependencies look up the digest.
The five-attempt/60-second login limiter is in-process and keyed by client host/email.
It is not a shared rate-limit service. The deployment currently starts one API worker.

## Authorization rules

`accessible()` answers **may this account access this contract?** Administrators
can access all contracts; technicians require an `Assignment`. `report_access()`
then answers **may this account write this report?** Only its current owner can
edit/close it or add/remove photographs. An admin must explicitly take/transfer
ownership first. Closed reports remain immutable.

Permanent report deletion has a separate rule: technicians may delete their own
drafts; administrators may delete drafts or closed reports. That broader deletion
privilege does not grant permission to edit another owner's answers.

## Persisted entities

| Table / model | Important relationship or constraint |
| --- | --- |
| `users` / User | Account identity, password encoding, `admin`/`tech` role. |
| `sessions` / SessionToken | Token digest -> user, with UTC expiry. |
| `contracts` / Contract | Company/year/semester/kind; generated name is unique. |
| `assignments` / Assignment | Composite `(user_id, contract_id)` access grant. |
| `chargers` / Charger | Unique `(contract_id, external_id)`; details preserve imported fields. |
| `reports` / Report | Charger, owner, revision, status, payload, frozen snapshot/checklist and dates. |
| `photos` / Photo | Globally unique photo UUID, report/checkpoint, original bytes, MIME and SHA-256 digest. |
| `deleted_reports` / DeletedReport | Permanent report tombstone, retaining charger/owner/state/deletion date. |
| `report_ownership_transfers` / OwnershipTransfer | Previous/new owner, acting admin and timestamp. `report_id` deliberately has no FK so history survives report deletion. |
| `import_profiles` / ImportProfile | Last committed mapping per company. |
| `imports` / ImportBatch | Import configuration, input digest and summary counts. |
| `audit` / Audit | User/action/target/time trail for general mutations. |

`create_all()` adds missing tables, including ownership history on an existing
installation. It does not migrate changed columns/constraints of an existing table.
Future schema changes need an explicit migration strategy; resetting a live database
is not a normal upgrade mechanism.

## Endpoints

Paths below are relative to the configured HTTPS server. Protected requests use
`Authorization: Bearer <token>`. For exact Pydantic schemas and response fields, read
[main.py](../../server/app/main.py); FastAPI also generates `/docs` and `/openapi.json`.

| Method and path | Purpose / access |
| --- | --- |
| `GET /api/health` | Public service health and version. |
| `POST /api/login`, `POST /api/logout` | Create/revoke session. Login body: email/password. |
| `GET /api/template` | Authenticated shared checklist. |
| `GET /api/users`, `POST /api/users` | Admin listing/creation; creation includes email/password/name/role. |
| `GET /api/contracts`, `POST /api/contracts` | List accessible contracts; admin creates company/year/semester/kind. |
| `PUT /api/contracts/{cid}/users/{user_id}` | Admin grants contract access; idempotent assignment. |
| `GET /api/contracts/{cid}/chargers` | Accessible catalogue with lightweight report summaries and deletion IDs. |
| `GET /api/companies/{company}/import-profile` | Admin retrieves saved mapping. |
| `POST /api/imports/analyse` | Admin multipart workbook analysis. |
| `POST /api/contracts/{cid}/import` | Admin multipart file/mapping/commit: preview or transactional upsert. |
| `GET /api/reports/{rid}/exists` | Read creation-recovery probe; distinguishes absent reports from deleted tombstones. |
| `GET /api/reports/{rid}` | Accessible report data and photo metadata, without image bytes. |
| `PUT /api/reports/{rid}` | Owner create/edit/close with charger_id/base_revision/status/payload. |
| `GET /api/reports/{rid}/owners` | Admin lists eligible contract technicians and admins. |
| `POST /api/reports/{rid}/owner` | Admin transfer with owner_id/base_revision; draft only. |
| `DELETE /api/reports/{rid}?base_revision=N` | Permanent authorized deletion, including photos and tombstone creation. |
| `PUT /api/reports/{rid}/photos/{pid}` | Owner multipart checkpoint/base_revision/file. |
| `DELETE /api/reports/{rid}/photos/{pid}?base_revision=N` | Owner removal from draft; acknowledged absence is idempotent. |
| `GET /api/reports/{rid}/photos/{pid}` | Accessible original JPEG/PNG bytes. |
| `GET /api/reports/{rid}/pdf`, `GET /api/reports/{rid}/bundle` | Accessible server PDF, or PDF/report JSON plus original photographs ZIP. |

A report response contains ID/charger/owner/technician/revision/status, payload,
snapshot, template, dates and photo metadata (`id`, checkpoint, filename, MIME,
digest, size). The catalogue deliberately returns less data in bulk; adding payloads
or image blobs there would hurt field startup/network/storage behavior.

## Transactions and revisions

New report PUTs require base revision zero. The server freezes current charger/
contract details and the checklist on creation. Existing PUTs require the current
owner and exact revision. The final conditional UPDATE includes report ID, owner
and revision; accepted writes increment revision. Closure additionally requires all
answers, notes for NOK, and required photos unless the answer is NA.

Creation/PUT/deletion/ownership transfer coordinate through a charger lock, with
report locks where needed. Photo mutations lock the report and recheck owner after
refreshing it. Ownership transfer increments revision and writes both its detailed
history and general audit action in the same transaction.

Photo upload does not increment report revision. It checks the supplied revision
before a new insertion; a repeated ID succeeds only when its report, checkpoint and
digest match the already stored photo. Real photo deletion increments revision;
an already absent photo returns success without incrementing twice. Final report
closure happens after the client's photo reconciliation.

Deletion stores a tombstone while removing photo/report rows. A later stale create
with the same UUID returns 410 instead of resurrecting the intervention. Admin
transfer does not silently merge another technician's offline data.

## Import and export details

Excel analysis/parsing uses openpyxl in read-only/data-only mode: no macro execution
or formula recalculation. The reader bounds uncompressed workbook size at 100 MiB
and total declared rows at 100,000; API uploads are bounded at 20 MiB. The Stations
profile validates exact headers for A-N and requires populated A/B/F/H/I/J/K/L/M/N.
Other layouts require an explicit unique-header mapping including `external_id`.
Simple numeric zero masks preserve leading zeroes in identifiers. Import errors
prevent any commit; valid records upsert only within the selected contract and retain
source columns in `details.source`.

Report payload JSON is limited to 300,000 serialized characters by the current
check. Answer strings/notes have an 8,000-character limit. Photographs must be
JPEG/PNG, at most 20 MiB per upload and 40 MP; the server bounds a report at 200
photographs and 250 MiB of original bytes.

PDF generation reads the frozen snapshot/checklist and escapes supplied text
before ReportLab markup. Dates display in Europe/Lisbon. Embedded images are
oriented/resized JPEG renditions; the database and ZIP preserve original bytes.

## Client compatibility (0.1.28)

`app/compatibility.py` parses numeric major.minor.patch releases and validates the
server-owned minimum/latest range at startup. All `/api/` operations except health
and the public compatibility endpoint require `X-Lem-Client-Version`; unsupported,
malformed, future and unidentified clients receive HTTP 426 with a structured policy
in `detail`. This runs before login/token issuance and also checks existing sessions.
The policy is a release gate, not proof of client integrity or an authorization credential.

`GET /api/client-compatibility` returns policy_version, current_version,
minimum_version, latest_version, compatible, update_available, update_url and message.
The client calls it before sending credentials and fails closed on malformed/missing
policy or network failure. Local report editing/export remains available without login.
`LEM_MIN_CLIENT_VERSION`, `LEM_LATEST_CLIENT_VERSION` and optional HTTPS
`LEM_UPDATE_URL` are passed by Compose. Restart/recreate the API after changing them.
See [operator instructions](../../../Common/docs/compatibility-0.1.28.md).

From 0.1.30, `GET /api/reports/{id}/pdf` and `/bundle` require a closed
report after the existing authorization check. Drafts return HTTP 409 before photos
are loaded or the document is generated. The client independently validates the
same state before the picker and again on acceptance; the local PDF renderer also
rejects drafts. A closed report may still export a local PDF without a session if
its complete snapshot and evidence are cached.


## 0.1.55: cadastro comum e checklist explícita

`stations` é a ficha física; `chargers` é o vínculo de contrato, conservando os
UUIDs anteriores. `details` usa os dados da estação, e `legacy_details` guarda
a importação antiga para recuperação. Os catálogos incluem `station_id`.
Cada PUT de report envia `checklist_version`. Novos reports usam a versão 4;
reports já existentes usam a sua versão congelada. No fecho, `station_update`
confirma os dados canónicos para os catálogos locais. Consulte as notas 0.1.55.
