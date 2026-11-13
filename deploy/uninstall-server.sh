#!/usr/bin/env bash
# Run locally on the server; Python performs validation and scoped removal.
set -euo pipefail
lem_script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
exec python3 "$lem_script_dir/uninstall-server.py" "$@"
