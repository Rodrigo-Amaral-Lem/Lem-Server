# Development and deployment

Run workstation commands from the **Server repository root**, which contains `server/`,
`shared/` and `deploy/`. Commands labelled **on the server** run from the selected installation
root, which has the same layout. Read [administration](administration.md) before operations
affecting live data.

## Local development

Use Python 3.12 (the container's runtime), a virtual environment and
[server/requirements.txt](../server/requirements.txt). `ffprobe` from FFmpeg is required for video
validation.

PDFs now use the same Qt 6.12 renderer and bundled DejaVu fonts as the application.
Install `PySide6-Essentials==6.12.0` from the requirements and ship the complete
`shared/report/` directory. On Debian/Ubuntu, the offscreen worker needs `libegl1`,
`libgl1`, `libxkbcommon0`, `libfontconfig1` and `libdbus-1-3`; the Dockerfile installs
them. It needs no display server and starts no visible Windows console. Rebuild
the server container when updating from the previous ReportLab renderer.

Existing contracts default to Lem. The additive `contract_report_styles` table is
created automatically; it does not require a reset or rewriting report answers.
Update the app and server together so offline and online PDFs share the same model.

Example in PowerShell 7.1+, from the Server repository root:

```powershell
python -m venv .venv
& ./.venv/Scripts/Activate.ps1
python -m pip install -r server/requirements.txt
New-Item -ItemType Directory -Force build | Out-Null
$env:DATABASE_URL = 'sqlite:///./build/lem-development.db'
$env:ADMIN_EMAIL = 'admin@example.test'
$env:ADMIN_NAME = 'Local administrator'
$env:ADMIN_PASSWORD = Read-Host 'Initial administrator password (12+ characters)' -MaskInput
$env:LEM_API_DOCS = '1'
python -m uvicorn run:app --app-dir server --host 127.0.0.1 --port 8000
```

The first account is created only when bootstrapping an empty users table. Changing `ADMIN_PASSWORD`
later does not reset an existing account; use the administration CLI. The SQLite file above is
relative to the repository root and stored under ignored `build/`. Reuse that same `DATABASE_URL`
when running local administration commands.

The normal client/server configuration uses HTTPS. The command above starts a loopback development
API, not a production endpoint. API documentation is public when enabled; leave it disabled in
production unless deliberately needed.

## Container layout and configuration

[compose.yml](../deploy/compose.yml) runs one PostgreSQL 17 service, one Python API worker and a
Caddy HTTPS proxy. The API/database are not published directly to the host; the proxy publishes
80/443. [Dockerfile](../deploy/Dockerfile) installs FFmpeg, copies `server/` and `shared/`, and
runs the API as user `lem`.

| Setting | Meaning |
| --- | --- |
| `LEM_DOMAIN` | Proxy hostname; default `lem.rodrod.pt` |
| `POSTGRES_PASSWORD` | Required database password; installer generates URL-safe hex |
| `ADMIN_EMAIL`, `ADMIN_NAME`, `ADMIN_PASSWORD` | Initial account only; password must have at least 12 characters |
| `LEM_MIN_CLIENT_VERSION`, `LEM_LATEST_CLIENT_VERSION` | Admitted version interval, defaults `0.9.1` / `0.11.0` |
| `LEM_UPDATE_URL` | Optional HTTPS update/download instructions URL |
| `LEM_EXPORT_CONCURRENCY`, `LEM_IMAGE_CONCURRENCY` | API admission slots; defaults 2/1, capped at 4/2 |
| `LEM_DOWNLOAD_CONCURRENCY` | Simultaneous original-evidence responses per worker; default 8, capped at 16 |
| `LEM_API_DOCS` | `1` enables public documentation/schema routes; default off |
| `LEM_TLS_DATA_VOLUME`, `LEM_TLS_CONFIG_VOLUME` | Existing named certificate/configuration volumes; preserve on update |

Outside Compose, `DATABASE_URL` selects the database and `CHECKLIST_FILE` can override the template
path. Compose fixes these for its services. The example configuration is
[deploy/.env.example](../deploy/.env.example); live `.env` files contain secrets and must not be
committed or pasted into logs.

Evidence download slots remain held until the response finishes, bounding in-flight BLOB memory;
database connections are released before sending. Busy admission returns 503 with `Retry-After`.
The app uses four evidence requests at once and retries temporary failures. Raising server slots
increases memory demand (each original can be up to 60 MiB); it is not a bandwidth setting.
Deploy the updated server for byte-range resume; updated clients safely restart against older servers.

## First installation — on the server

[install-ubuntu.sh](../deploy/install-ubuntu.sh) targets Ubuntu **24.04/26.04**, run as the non-root
installation account with sudo rights. Place the Server checkout/package in its intended permanent
directory first. Confirm domain DNS and free ports 80/443.

```bash
# From the installation root, on the server:
bash deploy/install-ubuntu.sh --user rodrigo_lem
```

The script detects Ubuntu, checks existing services/installation markers, prompts for the first
administrator's name/email, installs required host packages and Docker when appropriate, generates
private configuration, allows SSH/HTTP/HTTPS through UFW, creates TLS volumes, builds services and
checks HTTPS health.

It intentionally refuses to generate a new `.env` over an existing database/installation. It prints
the initial generated administrator password locally; record it securely. It can modify the host
firewall, packages, Docker services and volumes, so it is a first-install tool rather than a routine
update command.

`--user` selects the expected **Linux installation account**; it defaults to the current login.
Log in as that account first (for example `ssh rodrigo_lem@NEW_SERVER`), and do not use
`sudo bash` for the installer. A different selected account is rejected before host changes.
The account needs sudo rights and ownership of the installation directory, for example
`/home/rodrigo_lem/apps/Lem`. Set `LEM_DOMAIN` when using a different hostname:

```bash
LEM_DOMAIN=lem.example.com bash deploy/install-ubuntu.sh --user rodrigo_lem
```

The SSH/Linux user, application administrator and PostgreSQL role are separate identities.
Keep the internal database/container user `lem` in Compose and database commands; changing
the host login does not require renaming it. To transfer an existing database, follow
[Move a backup to a new server](administration.md#move-a-backup-to-a-new-server).
The workstation deployment frontends update an existing installation; they do not perform
the initial Ubuntu setup. Copy the Server source (`server/`, `shared/`, `deploy/` and
`.dockerignore`, without a live `.env` or local caches) to the permanent destination first.

Subsequent updates for this account use:

```powershell
& ./deploy/deploy.ps1 -ServerHost NEW_SERVER -UserName rodrigo_lem `
  -RemotePath /home/rodrigo_lem/apps/Lem -Domain lem.example.com
```

```bash
bash deploy/deploy.sh --host NEW_SERVER --user rodrigo_lem \
  --remote-path /home/rodrigo_lem/apps/Lem --domain lem.example.com
```

Account selection is installation tooling only: no client protocol or database schema changes,
so no client version increment or minimum-client increase is required.

## Update an existing installation — from a workstation

The PowerShell and Bash frontends build a server-only payload, transfer by SSH and delegate the
actual replacement to the remote helpers. They retain configured secrets and HTTPS volumes by
default. Use an explicit host/path when working with more than one installation.

```powershell
& ./deploy/deploy.ps1 -ServerHost lem.rodrod.pt -UserName lem `
  -RemotePath /home/lem/apps/Lem -Domain lem.rodrod.pt
```

```bash
bash deploy/deploy.sh --host lem.rodrod.pt --user lem \
  --remote-path /home/lem/apps/Lem --domain lem.rodrod.pt
```

| PowerShell | Bash | Meaning |
| --- | --- | --- |
| `-ServerHost`, `-UserName`, `-Port` | `--host`, `--user`, `--port` | SSH destination; defaults `lem.rodrod.pt`, `lem`, 22 |
| `-KeyPath` | `--key` | Optional identity file; otherwise use SSH configuration/agent |
| `-RemotePath` | `--remote-path` | Target path; `auto` requires exactly one matching installation under `~/apps` |
| `-Domain` | `--domain` | Assert the selected installation's domain; does not change its `.env` domain |
| `-ProjectPath` | `--project-path` | Server source root; normally resolved from the script location |
| `-PackageOnly` | `--package-only` | Write a deployment payload without contacting SSH |
| `-ResetDatabase` | `--reset-database` | Explicit destructive test-database reset; never a normal production update |

PowerShell supports Windows PowerShell 5.1/PowerShell 7 and needs SSH/SCP. Bash needs Python 3 plus
SSH/SCP. Host/domain/path arguments are validated; ambiguous auto-discovery stops rather than
choosing a folder by name.

Remote work validates payload checksum/paths, locks the target, inspects preserved
configuration/volumes, builds before stopping the API, takes a backup for a normal update, replaces
managed files and checks health. Failures can leave a partially completed deployment: review logs
and recovery state; do not assume transactional rollback.

`--package-only` is a transport/test feature, not a return to ZIP-based source control. If a payload
is needed, create an ignored `build/packages/` directory first and pass a destination such as
`build/packages/deployment.zip` to `-PackageOnly` / `--package-only`. Do not commit payloads.

## Tool catalogue and diagnosis

[deploy/README.md](../deploy/README.md) documents every public/internal deployment helper and its
side effects. For account commands, backup/restore, station imports and installation removal, use
[Administration](administration.md). For local payload tests without a remote host, use
[Testing](testing.md).

On the installed host, from its installation root, inspect
`docker compose --env-file deploy/.env -f deploy/compose.yml ps` and
`docker compose --env-file deploy/.env -f deploy/compose.yml logs --tail=80 api proxy`
(with sudo where required). Check DNS A/AAAA records,
domain, preserved `.env`, SSH identity and service health before repeating an interrupted
deployment. Avoid printing a fully expanded Compose configuration containing secrets;
`docker compose --env-file deploy/.env -f deploy/compose.yml config --quiet` validates without
displaying it.

### Upgrade to 0.5.0

Deploy the server before distributing the updated app. Startup updates the station catalogue
and its contract memberships. Existing reports keep their recorded identity and evidence.
Older clients can continue to sync using the previous report format. Update clients in place
to retain offline drafts, then sync them against the updated server. Checklist v7 remains current.

In 0.5.0 the default minimum client was 0.2.6 and the latest client was 0.5.0. Compatible older clients,
including 0.4.1, can log in and receive an update notice. Previously issued sessions keep their
existing expiry. Administrators can revoke sessions with the commands in the administration guide.

### Upgrade to 0.5.1

Update the API and its latest-client setting to 0.5.1 before updating the app in place. The
minimum remains 0.2.6. Startup creates the contract membership notes table. If an older import
captured **Notas** as raw metadata, startup recovers text with a recorded membership of origin
and removes it from the live shared station fields. For text retained only in a shared PCVE
record, reimport the source workbook into the intended contract.

### Upgrade to 0.5.3

Update the API and latest-client setting before installing client 0.5.3. Startup adds work-order planning, notification, cancellation and draft-recovery tables. On upgrade, the database uses the first recorded server save to associate ordinary drafts with visits that were resolved later. Legacy records without an audit entry use the stored start time. The minimum client remains 0.2.6. Existing contracts, full-contract assignments, reports and evidence keep their identifiers.

### Upgrade to 0.5.4

Coordinate the server and device update. Set both the minimum and latest compatible client to 0.5.4. Earlier clients omit the originating work item and can attribute a delayed report to a different visit. They receive an upgrade explanation at login and on authenticated requests. Preserve the existing application profile during installation so field staff can resume or recover pending edits.

### Upgrade to 0.6.0

Update the server and its configuration first. The deployment defaults admit clients from 0.5.5 to 0.6.0. A 0.5.5 client can sign in and receives the update notice. A 0.5.4 or earlier client receives the upgrade explanation at login and on authenticated requests. Startup creates the tables for corrective contracts, PCVE coverage, interventions and cancelled-order retention. Install Android code 188 or desktop 0.6.0 in place to preserve local reports.

### Upgrade to 0.6.1

Coordinate the API and device updates. The API accepts 0.6.1 and explains the required update
to older clients at login and on authenticated requests. App 0.6.0 displays a commissioning
task in a work order as preventive. Use the 0.6.1 defaults and install Android code 189 or the
desktop update in place to preserve local reports. This change uses the existing database
tables and checklist 7.

### Upgrade to 0.8.0

Update the API and its version settings before distributing Android code 191 or desktop 0.8.0.
The 0.6.1 client can sign in and sync commissioning work orders and receives the update notice.
Versions through 0.6.0 receive the required upgrade explanation at login and on authenticated requests.
Install the update over the existing client profile to retain local drafts and attachments.
