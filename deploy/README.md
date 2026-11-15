# Deployment tool reference

These scripts ship with the server payload. Run workstation commands from the **Server repository
root** using `deploy/...`; host commands run from the chosen installation root with the same layout. Full
source-checkout guidance is in [Deployment](../docs/deployment.md) and
[Administration](../docs/administration.md).

## Public entry points

| Script | Where / inputs | Output and side effects |
| --- | --- | --- |
| [deploy.ps1](deploy.ps1) | Windows PowerShell 5.1+/PowerShell 7 workstation; SSH destination, optional key/target/domain | Packages `server/shared/deploy`, transfers by SSH and updates the selected remote installation; `-PackageOnly FILE` avoids SSH |
| [deploy.sh](deploy.sh) | Bash workstation with Python 3 and SSH/SCP; `--help` lists flags | Same update pipeline; `--package-only FILE` writes only a payload; `--reset-database` is explicitly destructive |
| [install-ubuntu.sh](install-ubuntu.sh) | Fresh Ubuntu 24.04/26.04 host; non-root account with sudo, DNS/domain and free 80/443 | Installs/checks host dependencies, generates `.env`/credentials, changes firewall rules, creates volumes and starts services |
| [manage-server.sh](manage-server.sh) | Installed host with Docker access and `deploy/.env`; pass `help` or admin commands | Runs `app.admin_cli` inside the API container; commands can change accounts, ownership and catalogue data |
| [backup.sh](backup.sh) | Installed host with running database container | Private custom PostgreSQL dump under `deploy/backups`; includes database-stored evidence, not `.env`/TLS volumes |
| [uninstall-server.sh](uninstall-server.sh) | Installed Linux host; forwards arguments to Python | Default preview; explicit `--stop`/`--purge` follow [UNINSTALL.md](UNINSTALL.md) |
| [remove-prototype.py](remove-prototype.py) | New package outside the old installation; `--installation PATH` | Default plan; `--delete` removes the explicitly selected prototype and its resolved owned resources |

Default SSH destination is `lem@lem.rodrod.pt:22`; automatic target discovery is limited to an
unambiguous installation under `~/apps`. An explicit domain selects/asserts an existing
installation, not a hostname migration. Keep live configuration and backups private.

## Internal helpers

These are implementation interfaces used by the entry points, not alternative commands for routine
administration.

| Helper | Called by / contract |
| --- | --- |
| [deploy-package.py](deploy-package.py) | Bash frontend; positional `PROJECT OUTPUT`, requires a new output path, uses standard-library ZIP/hash logic and prints SHA-256. Excludes credentials, databases, caches and client source |
| [deploy-bootstrap.py](deploy-bootstrap.py) | SSH bootstrap; one base64 JSON configuration argument. Validates release/staging identity, checksum and archive paths before handing off remotely |
| [remote-deploy.sh](remote-deploy.sh) | Shared remote pipeline; positional `TARGET RESET RELEASE DOMAIN`. Validates/locks target, preserves configuration/volumes, builds, backs up or explicitly resets, replaces files and checks health |
| [find-installation.py](find-installation.py) | Imported helper `resolve_target(remote_path, home, domain)`; refuses ambiguous auto-discovery. No standalone CLI |
| [prepare-config.py](prepare-config.py) | Remote update helper; positional `OLD_ENV METADATA_JSON NEW_OUTPUT`; writes a new private configuration from existing settings/volume identities. `--domain ENV_FILE` reads only its domain. Do not log full configuration |
| [uninstall-server.py](uninstall-server.py) | Linux implementation behind the shell wrapper; installation-scoped ownership checks, lock, confirmation and optional fresh backup before removal |

The internal argument contracts are enforced in source and tests. Do not manually construct a
bootstrap payload or bypass frontend target validation to force an update.

## Related configuration

[compose.yml](compose.yml), [Dockerfile](Dockerfile), [Caddyfile](Caddyfile) and
[.env.example](.env.example) define the service layout. The API runs one worker; in-memory
admission/login limits are not shared across replicas. Server source and canonical resources remain
Git-managed even though a deployment uses a temporary transport ZIP.

A deployed payload intentionally omits the source repository's `docs/` directory; the transport
package includes this index and the removal guide. The current remote-update allowlist copies
UNINSTALL.md, but not README.md, into an existing installation; use source-checkout documentation if
that index is absent on the host. Detailed developer-guide links above resolve in a source checkout,
not in a minimal remote payload.
