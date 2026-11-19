#!/usr/bin/env bash
set -euo pipefail
entry=$(readlink -f -- "${BASH_SOURCE[0]}")
exec /usr/bin/python3 "${entry%/*}/lib/cli.py" "$@"
