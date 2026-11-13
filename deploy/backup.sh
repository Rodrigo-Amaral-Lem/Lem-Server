#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p backups
chmod 700 backups
umask 077
backup_file="backups/lem-$(date -u +%Y%m%dT%H%M%SZ).dump"
if [[ $EUID -eq 0 ]]; then lem_docker=(docker); else lem_docker=(sudo docker); fi
"${lem_docker[@]}" compose exec -T --interactive=false db pg_dump -U lem -d lem --format=custom < /dev/null > "$backup_file"
printf 'Backup completo (reports e fotografias): %s\n' "$backup_file"
