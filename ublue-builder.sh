#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    echo "python3 is required but was not found." >&2
    echo "Install it with your system package manager or Homebrew." >&2
    exit 1
fi

exec "$PYTHON_BIN" "${SCRIPT_DIR}/ublue_builder.py" "$@"
