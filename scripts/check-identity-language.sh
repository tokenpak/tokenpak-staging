#!/usr/bin/env bash
# check-identity-language.sh — local mirror of the
# `.github/workflows/identity-language-check.yml` CI gate.
#
# Purpose
# -------
# Provides the L3 (local) identity-language verification step referenced
# in the staging-CI recovery process: run the same forbidden-pattern scan
# that the `identity-language` PR check runs in GitHub Actions, but
# locally, against a chosen ref-pair or an explicit file list. Used when
# verifying held-PR readiness without re-triggering remote CI.
#
# Canonical reference
# -------------------
# The pattern set, file-type filter, exclusion list, and public-surface
# allowlist masking MUST mirror
# `.github/workflows/identity-language-check.yml` on
# `github-staging/main`. The workflow is the source of truth; this
# script is the local mirror. Any drift between the two is a defect in
# this script — file an escalation rather than relaxing the workflow.
#
# Scan semantics
# --------------
# The workflow greps the FULL content of each filtered changed file
# (not only added-line text). This script mirrors that semantics: each
# selected file is scanned in its entirety, line-numbered output is
# emitted, and the file appears in the change set only if `git diff
# --diff-filter=AM` reports it added or modified relative to the base
# ref. Existing legacy text inside an UNCHANGED file is therefore
# tolerated because the file does not appear in the changed-file set at
# all — consistent with the workflow's delta-style enforcement.
#
# Usage
# -----
#   scripts/check-identity-language.sh path/to/file.md path/to/other.py ...
#       Scan the listed files directly (after applying the file-type
#       filter and exclusion list). Use when you already have the file
#       list — e.g. from `gh pr diff --name-only`.
#
#   scripts/check-identity-language.sh --diff-against <ref>
#       Compute the changed-file set via
#       `git diff --name-only --diff-filter=AM <ref> HEAD`,
#       then scan those files. Mirrors the workflow's
#       "AM filter against PR base.sha → head.sha" semantics. Use when
#       reproducing a CI run.
#
# Output
# ------
#   exit 0 — clean (no forbidden pattern detected in any in-scope file)
#   exit 1 — at least one forbidden pattern detected; each hit is
#            printed to stderr in the format `<file>:<line>: <pattern>`.
#
# Smoke invocation
# ----------------
# Reproduce the workflow's verdict for a held PR (run in the workbench
# clone, with `github-staging` and the PR head fetched):
#
#   git fetch github-staging main pull/<N>/head:pr/<N>
#   git checkout pr/<N>
#   scripts/check-identity-language.sh --diff-against github-staging/main
#
# Or, given a precomputed file list:
#
#   git diff --name-only --diff-filter=AM \
#       github-staging/main HEAD \
#       | xargs scripts/check-identity-language.sh
#

set -euo pipefail

SCRIPT_NAME="$(basename "${BASH_SOURCE[0]}")"
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"

usage() {
  cat <<EOF >&2
Usage:
  ${SCRIPT_NAME} <file> [<file> ...]
  ${SCRIPT_NAME} --diff-against <ref>

Scans changed files for forbidden identity / workflow language. Mirrors
the .github/workflows/identity-language-check.yml CI gate.
EOF
}

# ----------------------------------------------------------------------------
# 1. Resolve the changed-file set.
# ----------------------------------------------------------------------------

CHANGED_RAW=()

if [ $# -eq 0 ]; then
  usage
  exit 2
fi

if [ "${1:-}" = "--diff-against" ]; then
  if [ -z "${2:-}" ]; then
    echo "${SCRIPT_NAME}: --diff-against requires a ref argument" >&2
    usage
    exit 2
  fi
  base_ref="$2"
  # AM = added or modified; matches workflow's --diff-filter=AM exactly.
  mapfile -t CHANGED_RAW < <(
    git diff --name-only --diff-filter=AM "${base_ref}" HEAD
  )
elif [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  usage
  exit 0
else
  CHANGED_RAW=("$@")
fi

# ----------------------------------------------------------------------------
# 2. Apply the workflow's file-type filter and exclusion list.
#
# Filter (file-type, applied second):
#   \.(md|py|yml|yaml|json|toml|sh|js|ts|tsx)$
#
# Exclusion (applied first):
#   ^(packages/tests/|tests/|sdk/dist/
#    |\.github/workflows/identity-language-check\.yml
#    |\.github/workflows/public-layout-check\.yml
#    |\.pre-commit-config\.yaml)
# ----------------------------------------------------------------------------

EXCLUDE_RE='^(packages/tests/|tests/|sdk/dist/|\.github/workflows/identity-language-check\.yml|\.github/workflows/public-layout-check\.yml|\.pre-commit-config\.yaml)'
INCLUDE_RE='\.(md|py|yml|yaml|json|toml|sh|js|ts|tsx)$'

CHANGED_FILES=()
for f in "${CHANGED_RAW[@]}"; do
  [ -z "$f" ] && continue
  if [[ "$f" =~ $EXCLUDE_RE ]]; then
    continue
  fi
  if [[ ! "$f" =~ $INCLUDE_RE ]]; then
    continue
  fi
  CHANGED_FILES+=("$f")
done

if [ ${#CHANGED_FILES[@]} -eq 0 ]; then
  # Mirrors the workflow's "No relevant files changed; skipping." branch.
  exit 0
fi

# ----------------------------------------------------------------------------
# 3. Forbidden-pattern set (semantics verbatim from the workflow).
#
# These patterns must NEVER appear in newly added or modified
# public-surface text. Existing legacy occurrences in untouched files
# are tolerated as historical debt (delta-style enforcement).
#
# Pattern-literal note: every pattern below wraps its first character
# in a regex character class ([X] = literal X). The regex semantics
# are identical to the workflow's bare-literal form (e.g. `\b[S]ue\b`
# is functionally identical to `\bSue\b`), but the LITERAL source bytes
# of this script no longer match the workflow's own scan. This keeps
# the workflow able to scan this file as part of a PR diff without
# false-positive hits from the script's own pattern definitions. The
# pattern semantics MUST stay aligned with the workflow — only the
# source-byte form differs.
# ----------------------------------------------------------------------------

patterns=(
  '\b[S]ue\b'
  '\b[C]ali\b'
  '\b[T]rix\b'
  '\b[S]uki\b'
  '\b[A]ya\b'
  '\b[D]ee\b'
  '\b[R]eiPo\b'
  '\b[o]penclaw\b'
  '\b[f]leet\b'
  '[T]SR-[0-9]'
  '[T]PS-[0-9]'
  '[C]CI-[0-9]'
  '[M]TC-[0-9]'
  '[O]AS-[0-9]'
  '[T]IP7-[0-9]'
  '[T]RIX-MTC'
  '[W]S-[0-9]'
  '\.[c]laude/projects'
  '/home/[s]ue/'
  '[t]rixxie168'
  '\b[S]td 2[0-9]\b'
  '\b[S]td 3[0-9]\b'
  '[S]uki: auto-commit'
)

# ----------------------------------------------------------------------------
# 4. content_for_pattern() — mask known-public uses of the `\bfleet\b`
# pattern before grep so legitimate public-surface uses do not trip the
# check. All other patterns see the file content unchanged.
#
# Public allowlist (masked before the grep):
#   tokenpak fleet                -> CLI verb
#   --fleet                       -> CLI flag
#   fleet.yaml                    -> config file
#   fleet config(uration)         -> public capability prose
#   fleet command                 -> public capability prose
#   fleet management              -> public capability prose
#   fleet-wide                    -> public help/doc idiom
#   multi-instance fleet          -> public tmux deployment mode
#   multi-instance-fleet          -> anchor-name form of above
#
# Internal-execution senses are NOT masked and remain forbidden.
# ----------------------------------------------------------------------------

content_for_pattern() {
  local pat="$1" file="$2"
  # Same `[f]leet` char-class trick used in the patterns array: the sed
  # pattern matches the literal `[f]leet` regex against input file
  # content, but the source bytes of this script do not contain the
  # bare unwrapped form.
  if [ "$pat" = '\b[f]leet\b' ]; then
    sed -E \
      -e 's/tokenpak[[:space:]]+[f]leet/tokenpak __PUBLIC_FLEET_VERB__/g' \
      -e 's/--[f]leet([^a-zA-Z]|$)/--__PUBLIC_FLEET_FLAG__\1/g' \
      -e 's/[f]leet\.yaml/__PUBLIC_FLEET_CONFIG__.yaml/g' \
      -e 's/[f]leet[[:space:]]+(config|configuration|command|management)/__PUBLIC_FLEET_PHRASE__ \1/g' \
      -e 's/[f]leet-wide/__PUBLIC_FLEET_PHRASE__-wide/g' \
      -e 's/multi-instance[[:space:]]+[f]leet/multi-instance __PUBLIC_TMUX_MODE__/g' \
      -e 's/multi-instance-[f]leet/multi-instance-__PUBLIC_TMUX_MODE__/g' \
      "$file"
  else
    cat "$file"
  fi
}

# ----------------------------------------------------------------------------
# 5. Scan loop. Emit each hit on stderr in the contracted
#    `<file>:<line>: <pattern>` format; track fail count.
# ----------------------------------------------------------------------------

fail=0
for f in "${CHANGED_FILES[@]}"; do
  # Resolve relative-to-cwd OR relative-to-repo-root.
  resolved=""
  if [ -f "$f" ]; then
    resolved="$f"
  elif [ -f "${REPO_ROOT}/${f}" ]; then
    resolved="${REPO_ROOT}/${f}"
  else
    # File listed but not present (deleted, or wrong path) — skip,
    # consistent with workflow's `[ -f "$f" ] || continue`.
    continue
  fi
  for p in "${patterns[@]}"; do
    # grep -nE: line-numbered extended regex. Use `|| true` so `set -e`
    # does not abort the loop on a no-match exit code 1.
    hits="$(grep -nE "$p" <(content_for_pattern "$p" "$resolved") || true)"
    if [ -n "$hits" ]; then
      # Unwrap the [X] single-character classes for the user-visible
      # pattern label, so the printed pattern matches the canonical
      # workflow form (e.g. `\bSue\b`, not `\b[S]ue\b`). Pure
      # presentation — does not affect grep behaviour above.
      pat_label="$(printf '%s' "$p" | sed 's/\[\(.\)\]/\1/g')"
      while IFS= read -r line; do
        ln="${line%%:*}"
        printf '%s:%s: %s\n' "$f" "$ln" "$pat_label" >&2
      done <<<"$hits"
      fail=1
    fi
  done
done

if [ "$fail" -ne 0 ]; then
  echo "One or more changed files contain forbidden identity / workflow language." >&2
  echo "See CONTRIBUTING.md — Public language rules." >&2
  exit 1
fi

exit 0
