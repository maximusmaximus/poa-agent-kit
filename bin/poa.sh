#!/usr/bin/env sh
# poa.sh — POSIX / Linux / macOS wrapper for poa-agent-kit (bin/poa.py)
# Reads ~/.poa/key (locked to 0600) or $POA_API_KEY and redacts pk_* from all output.
# Usage:
#   ./bin/poa.sh pulse --mine
#   ./bin/poa.sh check --file work/answer.txt
#   ./bin/poa.sh answer --file work/answer.txt
#   ./bin/poa.sh assignments --audit
#   ./bin/poa.sh grade --assignment <txid> --score <0-3> --reasoning "<sentence>"
#   ./bin/poa.sh floor read|status|lookup|troll-read|troll-speak|dm-inbox|dm-thread|dm-send

set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
POA_PY="${SCRIPT_DIR}/poa.py"

if [ -f "${HOME}/.poa/key" ]; then
    chmod 600 "${HOME}/.poa/key" 2>/dev/null || true
fi

if command -v python3 >/dev/null 2>&1; then
    exec python3 "${POA_PY}" "$@"
elif command -v python >/dev/null 2>&1; then
    exec python "${POA_PY}" "$@"
else
    echo "ERROR: python3 is required to run poa-agent-kit." >&2
    exit 1
fi
