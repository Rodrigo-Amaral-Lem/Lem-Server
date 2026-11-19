#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-only
# Part of LEM. See the accompanying LICENSE for license terms and warranty disclaimer.

# Host administration wrapper: manage-server.sh help, or no arguments for the interactive console.
# Requires Docker access and this installation's deploy/.env; forwards arguments inside the API container.
# Commands can mutate accounts, access, ownership and data. See docs/administration.md.
# To require a fresh login, use users revoke-sessions EMAIL_OR_ID or users revoke-sessions --all.

# Run on the server from an installed LEM checkout. No database port is exposed.
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ ! -f "$SCRIPT_DIR/.env" ]]; then
    echo "Falta deploy/.env. Execute esta ferramenta no projeto instalado no servidor." >&2
    exit 1
fi
if ! command -v docker >/dev/null 2>&1; then
    echo "Docker não encontrado." >&2
    exit 1
fi
tty_args=()
if [[ ! -t 0 || ! -t 1 ]]; then tty_args=(-T); fi
exec docker compose --env-file "$SCRIPT_DIR/.env" -f "$SCRIPT_DIR/compose.yml" \
    exec "${tty_args[@]}" api python -m app.admin_cli "$@"
