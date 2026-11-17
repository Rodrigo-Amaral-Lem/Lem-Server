# Test the API and deployment tools

Run from the **Server repository root**. Use an isolated `.venv/`, fictitious records and
disposable databases under ignored `build/`. Never point test runners at production.

## Dependencies and baseline command

Install [server/requirements.txt](../server/requirements.txt) plus `pytest`, `httpx`, `pypdf` and
`python-dotenv` (installer configuration round-trip tests).
Video tests need both `ffmpeg` (to generate real clips) and `ffprobe` (to inspect them) on PATH.
Check `ffmpeg -version` and `ffprobe -version` in the same shell used for pytest. Set `PYTHONPATH`
to the absolute `Server/server` directory:

```powershell
$env:PYTHONPATH = (Resolve-Path server).Path
python -m pytest server/tests deploy/tests -q -rs `
  --basetemp="$PWD/build/test-temp" -o "cache_dir=$PWD/build/pytest-cache"
```

Use a dedicated disposable `--basetemp`: pytest can clear that directory. An **absolute**
`cache_dir` prevents pytest from interpreting a relative value beneath its repository root. Put
other test reports and temporary outputs under `build/` too. Create that directory before
configuring any custom OS temporary paths.

## What the suites cover

| Area | Examples |
| --- | --- |
| Reports and ownership | Revision conflicts, lost retries, owner changes, deletion, closed-history protection |
| Contract/catalogue | Assignments, trash/retention, import batches, physical station corrections and rollback |
| Checklist | Manual readings/assessments, optional notes, actual photograph requirements and connector expansion |
| Media/export | Image/video admission, duplicate evidence IDs, signatures, PDF text/layout signals and ZIP originals |
| Access/security | Authentication, compatibility interval, request limits, permissions and resource admission |
| Deployment tools | Payload boundaries, safe extraction, configuration preservation, target discovery, update/removal behavior |

`test_release_alignment.py` inspects the sibling App repository, or `LEM_APP_REPO` if set to another
client repository root. It explicitly skips that cross-repository check when the client is
unavailable. The API/video tests otherwise use Server-owned fixture files under
`server/tests/fixtures`.

Some tests need PowerShell 7 (`PWSH` can select it), Bash/Linux facilities, Docker-command mocks, or
an external Stations workbook. Inspect `-rs` skip reasons. A skipped test is not a passing
deployment or a demonstration that production data is compatible.

The ten `test_stations_import.py` cases require `STATIONS_TEST_WORKBOOK` pointing to the populated
51-record `Import_Sheet_V0-2_Example.xlsm` and `STATIONS_TEST_TEMPLATE` pointing to the blank
`Common/Import_Sheet.xlsm`. Use absolute paths. These tests only read the originals and alter
in-memory copies. Their skip reasons concern missing workbook paths, independently of FFmpeg.

The remote deployment mocks are POSIX executable scripts, including the fake `ssh` and `docker`.
Run those tests on Linux with Bash and PowerShell 7; they are skipped on Windows to prevent the OS
from opening a mock file through a file association. Uninstallation tests also require `fcntl`.

## Focused checks

```powershell
python -m pytest server/tests/test_general_notes.py `
  server/tests/test_rules_057.py `
  server/tests/test_safety_layout.py -q `
  --basetemp="$PWD/build/rules-test-temp" -o "cache_dir=$PWD/build/pytest-cache"
```

Run CLI tests against a disposable database before changing account/ownership logic. Run
media/export tests when touching inspection, evidence references or rendering. Do not use the
destructive update/reset/uninstall commands as a way to test documentation examples.

`test_video_evidence.py` checks video upload retries, deletion, photograph requirements, PDF
generation and exact original bytes in ZIP exports. `test_video_formats.py` generates ten real clips covering
HEVC, 3GP/H.263, H.264 with audio, UHD 4K, the 120-second limit, excessive duration/resolution,
unsupported VP9, multiple video streams and audio-only files. Run both with `-rs` to distinguish
missing executables from failures. The format tests need FFmpeg encoders `libx264`, `libx265`,
`libvpx-vp9`, `h263` and `aac`; use a build that includes all five.

`test_export_concurrency.py` overlaps four real PDF/ZIP requests for the same and different reports,
using owners, another assigned technician and an administrator. It checks PDF text, JSON identity,
original evidence digests, overload while catalogue/sync requests continue, authorization,
trash/restore/deletion, rendering failures and interrupted ZIP streams followed by retry.
These API tests use disposable SQLite databases; they do not measure production capacity or
exercise PostgreSQL row-lock behavior.

`test_evidence_downloads.py` checks original byte ranges, suffixes, stale validators, invalid ranges,
authentication and report association. Its deliberately blocked ASGI sender verifies that slow
downloads release the database pool while holding a download admission slot, and that overload
does not block catalogues. The ZIP concurrency suite also counts original-BLOB queries to prevent
duplicate database reads during PDF/archive generation.

## Generated output and evidence

`test_contract_trash.py` exercises the 30-day gate with existing administrator/technician sessions,
including report creation/update/closure, existence checks and ranged evidence reads. Restoration
must preserve report revisions, answers and original evidence. `test_compatibility.py` checks
server-side login/session admission and update notices; `test_release_alignment.py` checks the
client/server version declarations. Run `deploy/tests/test_client_policy_config.py` on Linux because
it also asserts POSIX permissions on the generated environment file.

Some older workflow tests can write a demonstration PDF into `examples/`. Check Git status after
testing and restore/review that generated sample before staging an unrelated change. Workstation
payloads created by `-PackageOnly` / `--package-only` belong under ignored `build/packages/`.

Keep command transcripts, HTML/JSON test reports, screenshots and detailed audits in an ignored
directory such as `build/test-reports/`. A maintained guide should state a test's purpose, entry point and
prerequisites; individual dated test runs are evidence, not new documentation versions.

For client/native integration, use the [App test guide](../../App/docs/testing.md). Shared
maintenance and documentation checks are in [Common](../../Common/docs/maintenance.md).

`test_report_styles.py` checks default/changed issuers, import preview/commit, closed
report preservation, technician rejection and conflicting administrator revisions.
It also models a pre-branding database snapshot closed by a technician and exported by an
administrator, repeatedly switching LEM/CSMTEC and checking both PDF and ZIP without changing
the report's content, evidence, owner or revision.
`test_postgres_branding.py` repeats these cases against real PostgreSQL/psycopg, including the first
style insertion, a new login and server restart. Set `LEM_TEST_POSTGRES_URL` to a disposable
`postgresql+psycopg` database named `lem_branding_test` on loopback; non-loopback hosts and other
database names are rejected. Each case creates a random schema and drops only that schema during
cleanup. Never use a production database. Without the variable, its five cases explicitly skip;
SQLite success alone does not verify PostgreSQL INSERT result handling.
PDF tests use the actual isolated Qt worker: install the full requirements and Linux
offscreen libraries described in [deployment](deployment.md). The App's
`test_pdf_parity.py` compares native and server output for both brands. Run it when
changing the shared layout, painting bridges, fonts or Qt runtime version.

`test_report_corrections.py` checks desktop/admin/editor gates, cloned signature links, lost-response
retries, competing corrections, invalid answers/dates, cancellation racing commit, original deletion
racing copy creation, rollback after source deletion, retention and purge. `test_download_devices.py`
uses a real loopback HTTP server with four clients receiving the same 16-MiB original; two close
their sockets early and resume with ranges. It verifies digests, one shared BLOB read, released
connections/handles and cache eviction/failure recovery. Timings from this test are local transport
measurements, not production network benchmarks.

`test_correction_downloads.py` uses real loopback HTTP clients with SQLite and the opt-in isolated
PostgreSQL schema. A technician owns the closed original and downloads its evidence while an
administrator reads, stages and commits a correction. It holds 1 or 8 response bodies, disconnects
some devices, retries the commit, checks replacement links/ownership and compares original bytes.
Eight clients waiting on one cold-cache loader must not block report metadata or staging.
Slow PDF/ZIP workers must hold no DB connection: branding writes and correction commit finish
before the renderer is released, and the already accepted export retains the original snapshot.
Injected disk/rendering failures verify cleanup and preservation of both reports. These tests
separate preparation from transfer; they do not diagnose congestion on a production network.

`test_correction_identity.py` verifies that closed-report identity edits update all memberships of
the physical charger, preserve other historical reports, survive stale imports and are rolled back
with failed replacement. It covers staged/cancelled copies, legacy templates, newer conflicting or
unrelated values and idempotent retry after another intervention. `test_postgres_corrections.py`
repeats these checks and replacement/cancellation races using the opt-in PostgreSQL fixture above.
It also holds the catalogue lock in one transaction and requests correction commit in another;
the charger must remain available until the correction acquires the catalogue lock. This checks
lock ordering deterministically, without relying on a deadlock timeout.
Without that variable, ordinary API tests use disposable SQLite databases.

`test_general_notes.py` verifies exact ZIP originals for all seven optional notes fields, even with
empty/null text. Corrected old EST.18 cases inspect the photograph decoded from the PDF and retain
photo/video bytes through two corrections, including historical-signature references.

`test_maintenance_evidence.py` tests MAN.14–18 with OK/NOK readings, missing/present photographs,
N/A observations, old v6 draft upgrades and closed-history preservation. Real FFmpeg-generated
videos are accepted as attachments but explicitly fail the mandatory-photograph check.

`test_password_reset.py` checks administrator/technician role gates, stale role listings,
password bounds, preserved account/assignment data, secret-free audit/results and revocation of
all old sessions. Concurrent resets must leave one complete password and no old sessions;
administrators trying to reset each other must both be rejected without deadlock.
`test_postgres_password_reset.py` repeats the role/session/concurrency cases using the disposable
loopback-only PostgreSQL fixture and `LEM_TEST_POSTGRES_URL` described above.

## Checklist v7 regressions

Shared `maintenance-cases.json` covers trip endpoints and earth-resistance boundaries with exact
arithmetic. Exercise 6-digit integer mA grammar, legacy unit conversion/confirmation, untouched
suggestions versus custom notes, and closed-correction migration without losing photographs.
Server `test_maintenance_v7.py` also covers stale writers, incomplete correction rollback, repeated
commit, staged historical v6 upload and forged OK assessments.
Uploads before the next answers PUT must accept the already-projected current checkpoint;
invalid images and stale revisions must not persist a partial migration.
Versions 1–5 also cover safe correction migration, preservation of 8000-character values/notes,
historical signature evidence, required current-form completion and existing-copy GET/write parity.
Native measurement tests mirror boundary cases and verify local reload/closure behavior.
`test_form_evolution.py` adds GET/PUT parity, long merged observations through PDF/ZIP,
transactional reopening of rejected frozen uploads, and malformed/oversized-input rollback.
For its native projection comparisons, build App's `Lem-migration-projection` and set
`LEM_MIGRATION_BINARY` to the absolute executable path; missing native support is an explicit skip.
Keep the matching Qt runtime DLLs on PATH on Windows. The deterministic matrix includes current,
legacy, frozen, closed and unknown future versions, both normal and lost-ACK payloads, and future
connector rules. These checks complement rather than replace the multi-device integration suite.
UI changes require actual Android emulator
screenshots and touch exercises, including portrait, landscape and narrow layouts; offscreen tests
alone do not satisfy the user's instruction. Keep per-run evidence in ongoing_work.
