#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-only
# Part of LEM. See the accompanying LICENSE for license terms and warranty disclaimer.

# Public Linux host entry point: no flags previews, --stop retains data, --purge is destructive.
# Forwards all arguments to uninstall-server.py; see the adjacent UNINSTALL.md before removal.

# Run locally on the server; Python performs validation and scoped removal.
set -euo pipefail
lem_script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
exec python3 "$lem_script_dir/uninstall-server.py" "$@"
