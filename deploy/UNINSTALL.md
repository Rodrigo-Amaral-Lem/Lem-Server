# Stop or remove the Lem server

Run on the server, from the installation folder. Ubuntu Python 3 and Docker Compose are required.
The script uses sudo for Docker when needed; do not use the laptop SSH deployment script for this
operation.

If `.env` or other installation markers are missing, recovery mode automatically uses Docker
container labels to identify this exact installation path. It removes matching containers directly,
and only removes database/certificate volumes proven by their mounts. Volumes used by other
containers, volumes without ownership evidence, Docker networks and containers from other
installation paths are retained. If no matching containers remain, it can still remove the selected
installation files. Missing configuration no longer prevents this cleanup. A database backup is
attempted when an exclusively owned database volume and matching database container are found;
backup failure still requires an explicit `--skip-backup` to proceed.

Preview without changes:

```bash
bash deploy/uninstall-server.sh
```

Stop/remove containers and the Compose network, retaining installation files, database, evidence and
HTTPS certificates:

```bash
bash deploy/uninstall-server.sh --stop
```

Type `PARAR` when asked. To restart later, run from the installation root:

```bash
sudo docker compose --env-file deploy/.env -f deploy/compose.yml up -d
```

Remove containers, database/evidence, certificates and installation files:

```bash
bash deploy/uninstall-server.sh --purge
```

Type `APAGAR`, then the exact full installation path. Before removal, a new database backup is
made. Configuration and prior backups are retained in a private sibling directory named
`Lem-uninstall-backup-<timestamp>-<id>`. This directory contains credentials: keep it private. It
is not automatically deleted. Copy it off the server if it must survive server loss. HTTPS
certificates are deleted, not backed up by this script.

The database container must be running for the backup. Backup failure stops removal. Only if you
deliberately accept removal without a fresh backup, use `--purge --skip-backup`. Existing backups
and configuration are still retained. If Docker refuses a volume removal, installation files are
retained; review the error before retrying. Removal is not transactional.

To target another existing installation from this package:

```bash
bash deploy/uninstall-server.sh --installation /home/lem/Lem --purge
```

If the running project uses `/home/lem/Lem`, the script refuses to operate on a different copy at
`/home/lem/apps/Lem`. It does not remove Docker, Ubuntu packages, SSH accounts, DNS, firewall
rules, unrelated projects or Docker images. `--stop` retains certificates and configuration, so it
is not a clean reinstall. After `--purge`, use the first-install instructions with a fresh
extracted package; do not reuse a different database volume with a newly generated password.
