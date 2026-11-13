# Development and deployment

Run workstation commands from `ongoing_work`. Commands explicitly labelled **on the server** run in
the selected remote installation. Read [administration](administration.md) before operations
affecting live data.

## Local development

Use Python 3.12 (the container's runtime), a virtual environment and
[server/requirements.txt](../server/requirements.txt). `ffprobe` from FFmpeg is required for video
validation.

Example in PowerShell 7.1+, from `ongoing_work`:

```powershell
python -m venv server-venv
& ./server-venv/Scripts/Activate.ps1
python -m pip install -r ../Server/server/requirements.txt
$env:DATABASE_URL = 'sqlite:///./lem-development.db'
$env:ADMIN_EMAIL = 'admin@example.test'
$env:ADMIN_NAME = 'Local administrator'
$env:ADMIN_PASSWORD = Read-Host 'Initial administrator password (12+ characters)' -MaskInput
$env:LEM_API_DOCS = '1'
python -m uvicorn run:app --app-dir ../Server/server --host 127.0.0.1 --port 8000
```

The first account is created only when bootstrapping an empty users table. Changing `ADMIN_PASSWORD`
later does not reset an existing account; use the administration CLI. The SQLite file above is
relative to the current working directory, which is why this example starts in `ongoing_work`.

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
| `LEM_MIN_CLIENT_VERSION`, `LEM_LATEST_CLIENT_VERSION` | Admitted semantic-version interval; defaults are both `0.1.57` |
| `LEM_UPDATE_URL` | Optional HTTPS update/download instructions URL |
| `LEM_EXPORT_CONCURRENCY`, `LEM_IMAGE_CONCURRENCY` | API admission slots; defaults 2/1, capped at 4/2 |
| `LEM_API_DOCS` | `1` enables public documentation/schema routes; default off |
| `LEM_TLS_DATA_VOLUME`, `LEM_TLS_CONFIG_VOLUME` | Existing named certificate/configuration volumes; preserve on update |

Outside Compose, `DATABASE_URL` selects the database and `CHECKLIST_FILE` can override the template
path. Compose fixes these for its services. The example configuration is
[deploy/.env.example](../deploy/.env.example); live `.env` files contain secrets and must not be
committed or pasted into logs.

## First installation — on the server

[install-ubuntu.sh](../deploy/install-ubuntu.sh) targets Ubuntu **24.04/26.04**, run as the non-root
installation account with sudo rights. Place the Server checkout/package in its intended permanent
directory first. Confirm domain DNS and free ports 80/443.

```bash
# From the installation root, on the server:
bash deploy/install-ubuntu.sh
```

The script detects Ubuntu, checks existing services/installation markers, prompts for the first
administrator's name/email, installs required host packages and Docker when appropriate, generates
private configuration, allows SSH/HTTP/HTTPS through UFW, creates TLS volumes, builds services and
checks HTTPS health.

It intentionally refuses to generate a new `.env` over an existing database/installation. It prints
the initial generated administrator password locally; record it securely. It can modify the host
firewall, packages, Docker services and volumes, so it is a first-install tool rather than a routine
update command.

## Update an existing installation — from a workstation

The PowerShell and Bash frontends build a server-only payload, transfer by SSH and delegate the
actual replacement to the remote helpers. They retain configured secrets and HTTPS volumes by
default. Use an explicit host/path when working with more than one installation.

```powershell
& ../Server/deploy/deploy.ps1 -ServerHost lem.rodrod.pt -UserName lem `
  -RemotePath /home/lem/apps/Lem -Domain lem.rodrod.pt
```

```bash
bash ../Server/deploy/deploy.sh --host lem.rodrod.pt --user lem \
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
is needed, place it under `ongoing_work`, not the workspace root or Git repositories.

## Tool catalogue and diagnosis

[deploy/README.md](../deploy/README.md) documents every public/internal deployment helper and its
side effects. For account commands, backup/restore, station imports and installation removal, use
[Administration](administration.md). For local payload tests without a remote host, use
[Testing](testing.md).

On the installed host, `cd deploy` and inspect `docker compose ps` and
`docker compose logs --tail=80 api proxy` (with sudo where required). Check DNS A/AAAA records,
domain, preserved `.env`, SSH identity and service health before repeating an interrupted
deployment. Avoid printing a fully expanded Compose configuration containing secrets;
`docker compose config --quiet` validates without displaying it.
