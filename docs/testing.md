# Test the API and deployment tools

Run from the **Server repository root**. Use an isolated `.venv/`, fictitious records and
disposable databases under ignored `build/`. Never point test runners at production.

## Dependencies and baseline command

Install [server/requirements.txt](../server/requirements.txt) plus `pytest`, `httpx` and `pypdf`.
Video tests need `ffprobe` on PATH. Set `PYTHONPATH` to the absolute `Server/server` directory:

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

## Generated output and evidence

Some older workflow tests can write a demonstration PDF into `examples/`. Check Git status after
testing and restore/review that generated sample before staging an unrelated change. Workstation
payloads created by `-PackageOnly` / `--package-only` belong under ignored `build/packages/`.

Keep command transcripts, HTML/JSON test reports, screenshots and detailed audits in an ignored
directory such as `build/test-reports/`. A maintained guide should state a test's purpose, entry point and
prerequisites; individual dated test runs are evidence, not new documentation versions.

For client/native integration, use the [App test guide](../../App/docs/testing.md). Shared
maintenance and documentation checks are in [Common](../../Common/docs/maintenance.md).
