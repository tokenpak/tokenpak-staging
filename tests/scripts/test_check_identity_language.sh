#!/usr/bin/env bash
# test_check_identity_language.sh — smoke fixture for
# scripts/check-identity-language.sh.
#
# Lives in tests/scripts/ so the workflow's exclusion list keeps it from
# being scanned (the test fixture necessarily contains the forbidden
# patterns it asserts on).
#
# Acceptance coverage:
#   - AC-5: every pattern from the patterns array fires exactly once on
#           a synthetic file containing each pattern in isolation.
#   - AC-6: the public-allowlist fleet uses do NOT fire; the internal
#           fleet uses DO fire.
#
# Invocation:
#   bash tests/scripts/test_check_identity_language.sh
#
# Exits 0 on full pass, 1 on any assertion failure (failure detail
# printed to stderr).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
CHECKER="${REPO_ROOT}/scripts/check-identity-language.sh"

if [ ! -x "${CHECKER}" ]; then
  echo "FAIL: ${CHECKER} not executable / not found" >&2
  exit 1
fi

TMPDIR_TEST="$(mktemp -d /tmp/check-identity-language-test.XXXXXX)"
trap 'rm -rf "${TMPDIR_TEST}"' EXIT

pass=0
fail=0
note() { printf '  %s\n' "$*"; }
ok()   { pass=$((pass+1)); note "PASS — $*"; }
bad()  { fail=$((fail+1)); printf 'FAIL — %s\n' "$*" >&2; }

# ----------------------------------------------------------------------------
# Helper: run the checker on a list of files (cwd-relative) and capture
# exit code + stderr.
# ----------------------------------------------------------------------------
run_checker() {
  local stderr_file="$1"; shift
  local ec=0
  ( cd "${TMPDIR_TEST}" && "${CHECKER}" "$@" 2>"${stderr_file}" ) || ec=$?
  echo "${ec}"
}

# ----------------------------------------------------------------------------
# AC-5: every pattern fires exactly once on a synthetic file with each
# pattern in isolation, one per line.
#
# The patterns array below must stay in lockstep with the script's
# patterns array. Drift here = drift in the test fixture, NOT in the
# script's pattern set; treat any divergence as a fixture bug.
# ----------------------------------------------------------------------------

echo "[AC-5] synthetic-per-pattern fixture"

# Each entry is: a "trigger" line guaranteed to match the corresponding
# pattern when grep -E is applied. Order mirrors the script's patterns
# array exactly.
triggers=(
  'agent Sue authored the change'                             # \bSue\b
  'agent Cali ran the script'                                  # \bCali\b
  'agent Trix opened a PR'                                     # \bTrix\b
  'review by Suki'                                             # \bSuki\b
  'fixture line for Aya'                                       # \bAya\b
  'fixture line for Dee'                                       # \bDee\b
  'fixture line for ReiPo'                                     # \bReiPo\b
  'launched via openclaw'                                      # \bopenclaw\b
  'agent fleet orchestration step'                             # \bfleet\b (NOT masked)
  'reference TSR-1 in a changelog entry'                       # TSR-[0-9]
  'reference TPS-2 in a postmortem'                            # TPS-[0-9]
  'reference CCI-3 in a packet'                                # CCI-[0-9]
  'reference MTC-4 in a runbook'                               # MTC-[0-9]
  'reference OAS-5 in a ticket'                                # OAS-[0-9]
  'reference TIP7-6 in a memo'                                 # TIP7-[0-9]
  'reference TRIX-MTC literal token'                           # TRIX-MTC
  'reference WS-7 in a doc'                                    # WS-[0-9]
  'path .claude/projects/foo appears in trace'                 # \.claude/projects
  'path /home/sue/workspace/foo appears in log'                # /home/sue/
  'user handle trixxie168 appears'                             # trixxie168
  'see Std 25 for context'                                     # \bStd 2[0-9]\b
  'see Std 35 for context'                                     # \bStd 3[0-9]\b
  'Suki: auto-commit ran for this branch'                      # Suki: auto-commit
)

# Mirror of the patterns array order, used to assert per-pattern hits.
expected_patterns=(
  '\bSue\b'
  '\bCali\b'
  '\bTrix\b'
  '\bSuki\b'
  '\bAya\b'
  '\bDee\b'
  '\bReiPo\b'
  '\bopenclaw\b'
  '\bfleet\b'
  'TSR-[0-9]'
  'TPS-[0-9]'
  'CCI-[0-9]'
  'MTC-[0-9]'
  'OAS-[0-9]'
  'TIP7-[0-9]'
  'TRIX-MTC'
  'WS-[0-9]'
  '\.claude/projects'
  '/home/sue/'
  'trixxie168'
  '\bStd 2[0-9]\b'
  '\bStd 3[0-9]\b'
  'Suki: auto-commit'
)

if [ "${#triggers[@]}" -ne "${#expected_patterns[@]}" ]; then
  bad "trigger/expected arrays length mismatch: ${#triggers[@]} vs ${#expected_patterns[@]}"
  exit 1
fi

mkdir -p "${TMPDIR_TEST}/sample"
fixture="${TMPDIR_TEST}/sample/forbidden.md"
{ for t in "${triggers[@]}"; do echo "$t"; done; } > "${fixture}"

stderr_log="${TMPDIR_TEST}/stderr-ac5.log"
ec="$(run_checker "${stderr_log}" "sample/forbidden.md")"

if [ "${ec}" = "1" ]; then
  ok "exit code 1 on synthetic-per-pattern fixture"
else
  bad "exit code expected 1, got ${ec} on synthetic-per-pattern fixture"
fi

# Every expected pattern should appear at least once in the stderr log.
miss=0
for p in "${expected_patterns[@]}"; do
  # grep -F: fixed-string match against the literal pattern as printed.
  if ! grep -F -q -- ": ${p}" "${stderr_log}"; then
    bad "pattern not reported: ${p}"
    miss=$((miss+1))
  fi
done
if [ "${miss}" -eq 0 ]; then
  ok "every pattern reported at least once"
fi

# ----------------------------------------------------------------------------
# AC-6 (positive): public-surface fleet uses must NOT fire.
# ----------------------------------------------------------------------------

echo "[AC-6a] public fleet allowlist accepts legitimate uses"

mkdir -p "${TMPDIR_TEST}/sample-public"
public_fixture="${TMPDIR_TEST}/sample-public/public.md"
cat > "${public_fixture}" <<'EOF'
Run `tokenpak fleet status` to inspect cluster nodes.
Pass `--fleet primary` to scope output.
Edit `fleet.yaml` to register a new node.
The fleet management interface is documented in CLI Reference.
This setting is fleet-wide and applies to every node.
multi-instance fleet mode is described in the deployment guide.
The multi-instance-fleet anchor links to the same section.
EOF

stderr_log_pub="${TMPDIR_TEST}/stderr-ac6a.log"
ec="$(run_checker "${stderr_log_pub}" "sample-public/public.md")"
if [ "${ec}" = "0" ]; then
  ok "public allowlist uses do not trip the check"
else
  bad "public allowlist tripped the check (exit ${ec}); stderr:"
  sed 's/^/    | /' "${stderr_log_pub}" >&2
fi

# ----------------------------------------------------------------------------
# AC-6 (negative): internal fleet senses must fire.
# ----------------------------------------------------------------------------

echo "[AC-6b] internal fleet uses are still flagged"

mkdir -p "${TMPDIR_TEST}/sample-internal"
internal_fixture="${TMPDIR_TEST}/sample-internal/internal.md"
cat > "${internal_fixture}" <<'EOF'
The agent fleet is responsible for executing tasks.
A fleet worker picks up the next packet.
Coordination is handled by fleet orchestration.
EOF

stderr_log_int="${TMPDIR_TEST}/stderr-ac6b.log"
ec="$(run_checker "${stderr_log_int}" "sample-internal/internal.md")"
if [ "${ec}" = "1" ]; then
  ok "internal fleet uses are still flagged"
else
  bad "internal fleet uses NOT flagged (exit ${ec})"
fi

# Expect three hits, one per line.
internal_hits="$(grep -c -F ': \bfleet\b' "${stderr_log_int}" || true)"
if [ "${internal_hits}" = "3" ]; then
  ok "internal fixture produced 3 \\bfleet\\b hits"
else
  bad "internal fixture produced ${internal_hits} \\bfleet\\b hits (expected 3)"
fi

# ----------------------------------------------------------------------------
# Empty input case: no files passed → usage / exit 2; ALL-excluded input
# → exit 0 (mirrors "no relevant files changed; skipping").
# ----------------------------------------------------------------------------

echo "[empty] zero-file input"

mkdir -p "${TMPDIR_TEST}/sample-empty"
ec="$(run_checker "${TMPDIR_TEST}/stderr-empty1.log")"   # no args
if [ "${ec}" = "2" ]; then
  ok "no args → exit 2 (usage)"
else
  bad "no args → exit ${ec} (expected 2)"
fi

# All-excluded: pass a tests/ path that exists.
mkdir -p "${TMPDIR_TEST}/tests"
echo "agent Sue appears here but should be excluded" > "${TMPDIR_TEST}/tests/sample.md"
ec="$(run_checker "${TMPDIR_TEST}/stderr-empty2.log" "tests/sample.md")"
if [ "${ec}" = "0" ]; then
  ok "tests/-excluded path → exit 0"
else
  bad "tests/-excluded path → exit ${ec} (expected 0)"
fi

# ----------------------------------------------------------------------------
# Summary.
# ----------------------------------------------------------------------------

echo
echo "summary: ${pass} pass, ${fail} fail"
if [ "${fail}" -gt 0 ]; then
  exit 1
fi
exit 0
