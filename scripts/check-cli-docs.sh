#!/usr/bin/env bash
# check-cli-docs.sh — CI gate: regenerate CLI docs and diff against committed file.
# Exit 1 (hard fail) if the committed docs/cli-reference.md is out of date.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
COMMITTED="${REPO_ROOT}/docs/cli-reference.md"
GENERATED="$(mktemp /tmp/cli-reference-XXXXXX.md)"

cleanup() { rm -f "${GENERATED}"; }
trap cleanup EXIT

PYTHON_BIN="${PYTHON:-}"
if [[ -z "${PYTHON_BIN}" ]]; then
    if command -v python3 >/dev/null 2>&1; then
        PYTHON_BIN="python3"
    elif command -v python >/dev/null 2>&1; then
        PYTHON_BIN="python"
    else
        echo "ERROR: neither python3 nor python is available." >&2
        exit 127
    fi
fi

echo "Generating CLI reference from tokenpak/cli.py..."
"${PYTHON_BIN}" "${SCRIPT_DIR}/generate-cli-docs.py" --stdout > "${GENERATED}"

if diff -u "${COMMITTED}" "${GENERATED}"; then
    echo "OK: docs/cli-reference.md is up to date."
    exit 0
else
    echo ""
    echo "FAIL: docs/cli-reference.md is out of date."
    echo "Re-run:  ${PYTHON_BIN} scripts/generate-cli-docs.py"
    echo "Then commit the updated docs/cli-reference.md."
    exit 1
fi
