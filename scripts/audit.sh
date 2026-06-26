#!/usr/bin/env bash
# audit.sh — `make audit` driver.
#
# Bundles the deterministic local audit checks specified by the 2026-04-29
# product-hardening proposal S1.2:
#
#   1. pytest -m quick           (already shipped — S1.1)
#   2. CLI help smoke            (python3 -m tokenpak --help)
#   3. Command inventory check   (scripts/check-cli-docs.sh)
#   4. Docs inventory check      (scripts/audit-docs.sh)
#   5. Package contents dry-run  (scripts/audit_package_dryrun.py)
#   6. Public-safety leak check  (scripts/audit_internal_leakage.py)
#
# No live provider calls. No network. No running proxy required.
# Exit codes: 0 = all checks pass; non-zero = at least one check failed.
#
# Usage:
#   bash scripts/audit.sh
#   PYTHON=.venv/bin/python3 bash scripts/audit.sh
#   PYTEST=.venv/bin/pytest bash scripts/audit.sh
#
# Override individual checks with SKIP_<NAME>=1 if you only need a subset:
#   SKIP_QUICK=1 SKIP_LEAK=1 bash scripts/audit.sh

set -u
set -o pipefail

PYTHON="${PYTHON:-python3}"
PYTEST="${PYTEST:-pytest}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# Track per-check status. Each entry: "name|status|note".
results=()
failures=0

run_check() {
    local name="$1"
    shift
    local skip_var="SKIP_${name//-/_}"
    skip_var="${skip_var^^}"
    if [ "${!skip_var:-0}" = "1" ]; then
        printf '\n--- [%s] SKIP (via %s=1) ---\n' "$name" "$skip_var"
        results+=("$name|skip|$skip_var=1")
        return 0
    fi
    printf '\n--- [%s] RUN: %s ---\n' "$name" "$*"
    if "$@"; then
        results+=("$name|pass|")
        return 0
    else
        local rc=$?
        results+=("$name|fail|exit=$rc")
        failures=$((failures + 1))
        return $rc
    fi
}

# 1. pytest -m quick
run_check quick "$PYTEST" -m quick -q --tb=short || true

# 2. CLI help smoke
run_check cli-help "$PYTHON" -m tokenpak --help >/dev/null || true

# 3. Command inventory check (regenerates docs/cli-reference.md and diffs)
if [ -x scripts/check-cli-docs.sh ]; then
    run_check cli-docs bash scripts/check-cli-docs.sh || true
else
    results+=("cli-docs|skip|scripts/check-cli-docs.sh missing")
fi

# 4. Docs inventory check (top-level + nav drift detection)
if [ -x scripts/audit-docs.sh ]; then
    run_check docs-inventory bash scripts/audit-docs.sh || true
else
    results+=("docs-inventory|skip|scripts/audit-docs.sh missing")
fi

# 5. Package contents dry-run validation
run_check package-dryrun "$PYTHON" scripts/audit_package_dryrun.py --root "$REPO_ROOT" || true

# 6. Public-safety leakage check
run_check leak "$PYTHON" scripts/audit_internal_leakage.py --root "$REPO_ROOT" || true

# ── Summary ───────────────────────────────────────────────────────────────────
printf '\n========== Audit Summary ==========\n'
printf '%-22s %-6s %s\n' "check" "status" "note"
printf -- '----------------------- ------ ----------------------------------\n'
for r in "${results[@]}"; do
    name="${r%%|*}"
    rest="${r#*|}"
    status="${rest%%|*}"
    note="${rest#*|}"
    printf '%-22s %-6s %s\n' "$name" "$status" "$note"
done
printf -- '-----------------------------------------------------------------\n'

if [ "$failures" -gt 0 ]; then
    printf 'AUDIT FAIL — %d check(s) failed.\n' "$failures"
    exit 1
fi
printf 'AUDIT PASS — all checks ok.\n'
exit 0
