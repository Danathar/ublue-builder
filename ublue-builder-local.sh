#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    echo "python3 is required but was not found." >&2
    echo "Install it with your system package manager or Homebrew." >&2
    exit 1
fi

if ! "$PYTHON_BIN" - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
PY
then
    echo "python3.10 or newer is required." >&2
    echo "Install a newer Python and re-run this script." >&2
    exit 1
fi

exec "$PYTHON_BIN" "${SCRIPT_DIR}/ublue_builder_local.py" "$@"
