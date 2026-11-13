#!/usr/bin/env bash
# Host tool: run from an installed checkout; requires a running Compose database.
# Writes a private custom-format pg_dump under deploy/backups, including database-stored evidence.
# Does not back up .env or certificate volumes. See docs/administration.md in a source checkout.

set -euo pipefail
cd "$(dirname "$0")"
mkdir -p backups
chmod 700 backups
umask 077
backup_file="backups/lem-$(date -u +%Y%m%dT%H%M%SZ).dump"
if [[ $EUID -eq 0 ]]; then lem_docker=(docker); else lem_docker=(sudo docker); fi
"${lem_docker[@]}" compose exec -T --interactive=false db pg_dump -U lem -d lem --format=custom < /dev/null > "$backup_file"
printf 'Backup completo (reports e fotografias): %s\n' "$backup_file"
