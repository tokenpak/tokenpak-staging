#!/usr/bin/env bash
# promote-staging-to-public.sh — governed staging→public PREPARE-AND-VERIFY path.
#
# Encodes the governed staging-to-public re-anchor mechanic so a promotion does
# not have to be hand-driven each time. This script *prepares and verifies* a re-anchor of
# approved staging content onto current public main, then STOPS. It never pushes
# to public, never tags, never publishes, never dispatches a release workflow,
# never sets PROMOTE_PUBLIC_ALLOW, never rotates credentials, and never touches
# branch protection. The actual public push stays a separate human approval
# step over the exact prepared SHA.
#
# What it does:
#   1. Re-anchors the approved staging commit onto current public main by
#      cherry-pick (never from a `shared` / tokenpak-origin divergent lineage).
#   2. Asserts author AND committer == "TokenPak <hello@tokenpak.ai>".
#   3. Verifies the result is a linear fast-forward candidate of public main
#      (single parent == public base; no merge commit / squash / web-button).
#   4. Runs the repository's leak + identity-surface check if available locally;
#      otherwise fails closed with the exact command to run.
#   5. Detects already-promoted content (empty re-anchor) and exits cleanly.
#   6. Prints the prepared SHA + the exact human approval push command
#      and STOPS.
#
# Usage:
#   scripts/promote-staging-to-public.sh --staging-ref <sha|ref> [options]
#
# Options:
#   --staging-ref REF      Approved staging content commit (PR merge/head). REQUIRED.
#   --public-ref REF       Current public main base. Default: <public-remote>/<public-branch>.
#   --public-remote NAME   Public remote. Default: auto-detect (github, then origin).
#   --public-branch NAME   Public branch. Default: main.
#   --staging-remote NAME  Staging remote. Default: github-staging.
#   --staging-branch NAME  Staging branch (sanctioned lineage). Default: main.
#   --prepared-branch NAME Local branch to leave the prepared commit on for the human push.
#                          Default: promote/public-prepared.
#   --leak-check PATH      Leak/identity-surface scanner. Default: scripts/release_gate/check_release_leaks.py
#   --leak-tree DIR        Tree the scanner scans. Default: tokenpak
#   --expected-identity S  Required author+committer identity. Default: "TokenPak <hello@tokenpak.ai>".
#   --repo DIR             Operate in this git repo. Default: current directory.
#   --no-fetch             Skip fetching the public/staging remotes (offline / tests).
#   -h, --help             Show this help and exit.
#
# Exit status:
#   0  prepared+verified OK, or content already promoted (both clean — see RESULT line)
#   2  usage error
#   3  identity mismatch (author or committer not TokenPak, or co-authored-by trailer)
#   4  not a clean fast-forward candidate (cherry-pick conflict / merge commit)
#   5  divergent lineage / forbidden source remote
#   6  required local check missing (leak/identity scanner not runnable)
#   7  leak / forbidden public surface found
#   8  setup error (not a git repo, refs missing, etc.)

set -euo pipefail

# ── Exit-code constants ─────────────────────────────────────────────────────
readonly EX_OK=0
readonly EX_USAGE=2
readonly EX_IDENTITY=3
readonly EX_NONFF=4
readonly EX_DIVERGENT=5
readonly EX_MISSING_CHECK=6
readonly EX_LEAK=7
readonly EX_SETUP=8

# ── Defaults ────────────────────────────────────────────────────────────────
STAGING_REF=""
PUBLIC_REF=""
PUBLIC_REMOTE=""
PUBLIC_BRANCH="main"
STAGING_REMOTE="github-staging"
STAGING_BRANCH="main"
PREPARED_BRANCH="promote/public-prepared"
LEAK_CHECK="scripts/release_gate/check_release_leaks.py"
LEAK_TREE="tokenpak"
EXPECTED_IDENTITY="TokenPak <hello@tokenpak.ai>"
REPO_DIR="."
DO_FETCH=1

# ── Output helpers ──────────────────────────────────────────────────────────
if [[ -t 1 ]]; then
  C_RED=$'\033[0;31m'; C_GRN=$'\033[0;32m'; C_YEL=$'\033[0;33m'; C_NC=$'\033[0m'
else
  C_RED=""; C_GRN=""; C_YEL=""; C_NC=""
fi
info() { printf '%s\n' "$*"; }
ok()   { printf '%s✅ %s%s\n' "$C_GRN" "$*" "$C_NC"; }
warn() { printf '%s⚠️  %s%s\n' "$C_YEL" "$*" "$C_NC" >&2; }
# die CODE MESSAGE — print error + STOP marker, never push.
die() { local code="$1"; shift; printf '%s❌ %s%s\n' "$C_RED" "$*" "$C_NC" >&2; exit "$code"; }

usage() { sed -n '2,60p' "$0" | sed 's/^# \{0,1\}//'; }

# ── Worktree cleanup (always; never leaves scratch state behind) ────────────
WORKTREE_DIR=""
WT_PARENT=""
# shellcheck disable=SC2317  # invoked by the EXIT trap.
cleanup() {
  if [[ -n "$WORKTREE_DIR" && -d "$WORKTREE_DIR" ]]; then
    command git -C "$REPO_DIR" worktree remove --force "$WORKTREE_DIR" >/dev/null 2>&1 || true
  fi
  [[ -n "$WT_PARENT" && -d "$WT_PARENT" ]] && rm -rf "$WT_PARENT"
  [[ -n "${LEAK_LOG:-}" && -f "${LEAK_LOG:-}" ]] && rm -f "$LEAK_LOG"
  return 0
}
trap cleanup EXIT

# ── Arg parse ───────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --staging-ref)     STAGING_REF="${2:-}"; shift 2 ;;
    --public-ref)      PUBLIC_REF="${2:-}"; shift 2 ;;
    --public-remote)   PUBLIC_REMOTE="${2:-}"; shift 2 ;;
    --public-branch)   PUBLIC_BRANCH="${2:-}"; shift 2 ;;
    --staging-remote)  STAGING_REMOTE="${2:-}"; shift 2 ;;
    --staging-branch)  STAGING_BRANCH="${2:-}"; shift 2 ;;
    --prepared-branch) PREPARED_BRANCH="${2:-}"; shift 2 ;;
    --leak-check)      LEAK_CHECK="${2:-}"; shift 2 ;;
    --leak-tree)       LEAK_TREE="${2:-}"; shift 2 ;;
    --expected-identity) EXPECTED_IDENTITY="${2:-}"; shift 2 ;;
    --repo)            REPO_DIR="${2:-}"; shift 2 ;;
    --no-fetch)        DO_FETCH=0; shift ;;
    -h|--help)         usage; exit "$EX_OK" ;;
    *) die "$EX_USAGE" "unknown argument: $1 (try --help)" ;;
  esac
done

[[ -n "$STAGING_REF" ]] || die "$EX_USAGE" "--staging-ref is required (try --help)"

# ── Repo sanity ─────────────────────────────────────────────────────────────
git -C "$REPO_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1 \
  || die "$EX_SETUP" "not a git work tree: $REPO_DIR"
REPO_DIR="$(git -C "$REPO_DIR" rev-parse --show-toplevel)"

git() { command git -C "$REPO_DIR" "$@"; }  # all git runs in the target repo

# ── Divergent-lineage / forbidden-source guard ──────────────────────────────
# A public promotion must re-anchor against the sanctioned staging lineage —
# NEVER promote content carried on `shared` / ~/tokenpak-origin.git divergent
# lineage. Reject a forbidden source remote by name or URL before doing work.
is_forbidden_remote() {
  local name="$1" url=""
  [[ "$name" == "shared" ]] && return 0
  url="$(git remote get-url "$name" 2>/dev/null || true)"
  if [[ "$url" =~ tokenpak-origin\.git ]] || [[ "$url" =~ /origin\.git$ ]]; then
    return 0
  fi
  return 1
}
# Reject a staging-ref namespaced under a forbidden remote (e.g. "shared/main").
case "$STAGING_REF" in
  shared/*) die "$EX_DIVERGENT" "refusing divergent lineage: staging-ref '$STAGING_REF' is on the 'shared' hub; re-anchor against $STAGING_REMOTE/$STAGING_BRANCH" ;;
esac
if is_forbidden_remote "$STAGING_REMOTE"; then
  die "$EX_DIVERGENT" "refusing divergent lineage: staging remote '$STAGING_REMOTE' resolves to a shared/tokenpak-origin hub; promote only from the sanctioned staging remote"
fi

# ── Resolve public remote (auto-detect github → origin) ─────────────────────
if [[ -z "$PUBLIC_REMOTE" ]]; then
  if git remote get-url github >/dev/null 2>&1; then
    PUBLIC_REMOTE="github"
  elif git remote get-url origin >/dev/null 2>&1; then
    PUBLIC_REMOTE="origin"
  fi
fi
[[ -z "$PUBLIC_REF" && -n "$PUBLIC_REMOTE" ]] && PUBLIC_REF="${PUBLIC_REMOTE}/${PUBLIC_BRANCH}"
[[ -n "$PUBLIC_REF" ]] || die "$EX_SETUP" "could not determine public base ref (no github/origin remote and no --public-ref)"

# ── Fetch (freshness) unless disabled ───────────────────────────────────────
if [[ "$DO_FETCH" -eq 1 ]]; then
  [[ -n "$PUBLIC_REMOTE" ]] && { git fetch --quiet "$PUBLIC_REMOTE" "$PUBLIC_BRANCH" 2>/dev/null || warn "fetch of $PUBLIC_REMOTE failed; verifying against local ref"; }
  git remote get-url "$STAGING_REMOTE" >/dev/null 2>&1 && { git fetch --quiet "$STAGING_REMOTE" 2>/dev/null || warn "fetch of $STAGING_REMOTE failed; verifying against local ref"; }
fi

# ── Resolve SHAs ────────────────────────────────────────────────────────────
PUBLIC_SHA="$(git rev-parse --verify --quiet "${PUBLIC_REF}^{commit}" 2>/dev/null || true)"
[[ -n "$PUBLIC_SHA" ]] || die "$EX_SETUP" "cannot resolve public base ref: $PUBLIC_REF"
STAGING_SHA="$(git rev-parse --verify --quiet "${STAGING_REF}^{commit}" 2>/dev/null || true)"
[[ -n "$STAGING_SHA" ]] || die "$EX_SETUP" "cannot resolve staging ref: $STAGING_REF"

info "────────────────────────────────────────────────────────────"
info "  promote-staging-to-public.sh — PREPARE + VERIFY (no push)"
info "  staging content : ${STAGING_SHA:0:12}  (${STAGING_REF})"
info "  public base     : ${PUBLIC_SHA:0:12}  (${PUBLIC_REF})"
info "  expected identity: ${EXPECTED_IDENTITY}"
info "────────────────────────────────────────────────────────────"

# ── Prepare the re-anchor in an isolated worktree (no effect on checkout) ────
# Use a fresh, non-existent subdir under a temp parent: `git worktree add`
# requires the target path not to pre-exist.
WT_PARENT="$(mktemp -d "${TMPDIR:-/tmp}/promote-public.XXXXXX")"
WORKTREE_DIR="${WT_PARENT}/wt"
git worktree add --detach --quiet "$WORKTREE_DIR" "$PUBLIC_SHA" \
  || die "$EX_SETUP" "could not create scratch worktree at public base"

wt() { command git -C "$WORKTREE_DIR" "$@"; }

# Stage the staging content onto public base without committing yet, so empty
# (already-promoted) and conflict (non-ff) outcomes are first-class.
if ! wt cherry-pick --no-commit "$STAGING_SHA" >/dev/null 2>&1; then
  wt cherry-pick --abort >/dev/null 2>&1 || true
  die "$EX_NONFF" "cannot cleanly re-anchor ${STAGING_SHA:0:12} onto public main ${PUBLIC_SHA:0:12} (conflict): public has diverged on these paths — not a clean fast-forward candidate. Rebase/resolve before promoting."
fi

if wt diff --cached --quiet; then
  ok "RESULT: already-promoted — staging content ${STAGING_SHA:0:12} is already present on public main ${PUBLIC_SHA:0:12}; no new commit needed."
  info "Nothing to push. STOP."
  exit "$EX_OK"
fi

# Commit the re-anchor, preserving the staging author + message, forcing the
# committer identity to TokenPak. (Author is verified below — a non-TokenPak
# author is exactly the identity-mismatch case we must reject.)
EXP_NAME="${EXPECTED_IDENTITY% <*}"
EXP_EMAIL="${EXPECTED_IDENTITY##*<}"; EXP_EMAIL="${EXP_EMAIL%>}"
command git -C "$WORKTREE_DIR" \
  -c "user.name=${EXP_NAME}" -c "user.email=${EXP_EMAIL}" \
  commit --quiet -C "$STAGING_SHA" \
  || die "$EX_SETUP" "failed to create re-anchored commit"

CAND_SHA="$(wt rev-parse HEAD)"

# ── Verify: linear fast-forward candidate (single parent == public base) ────
PARENTS="$(wt rev-list --parents -n1 "$CAND_SHA")"
# Format: "<cand> <parent1> [<parent2> ...]"
read -r _self P1 P2 _rest <<<"$PARENTS"
if [[ -n "${P2:-}" ]]; then
  die "$EX_NONFF" "prepared commit ${CAND_SHA:0:12} is a merge commit (>1 parent) — public promotion must be a linear fast-forward, no merge/squash/web-button"
fi
if [[ "$P1" != "$PUBLIC_SHA" ]]; then
  die "$EX_NONFF" "prepared commit parent ${P1:0:12} != current public main ${PUBLIC_SHA:0:12} — not a clean fast-forward candidate"
fi
ok "linear fast-forward candidate (single parent == public main)"

# ── Verify: author + committer identity ─────────────────────────────────────
CAND_AUTHOR="$(wt show -s --format='%an <%ae>' "$CAND_SHA")"
CAND_COMMITTER="$(wt show -s --format='%cn <%ce>' "$CAND_SHA")"
if [[ "$CAND_AUTHOR" != "$EXPECTED_IDENTITY" ]]; then
  die "$EX_IDENTITY" "author identity mismatch: '$CAND_AUTHOR' != '$EXPECTED_IDENTITY' — staging content must be authored by TokenPak before promotion"
fi
if [[ "$CAND_COMMITTER" != "$EXPECTED_IDENTITY" ]]; then
  die "$EX_IDENTITY" "committer identity mismatch: '$CAND_COMMITTER' != '$EXPECTED_IDENTITY'"
fi
if wt show -s --format='%B' "$CAND_SHA" | grep -qiE '^co-authored-by:'; then
  die "$EX_IDENTITY" "prepared commit carries a Co-authored-by trailer — forbidden on public TokenPak commits"
fi
ok "author + committer == ${EXPECTED_IDENTITY}; no co-authored-by trailer"

# ── Run the local leak / identity-surface check, or fail closed ─────────────
LEAK_CHECK_ABS="$LEAK_CHECK"
[[ "$LEAK_CHECK_ABS" = /* ]] || LEAK_CHECK_ABS="${REPO_DIR}/${LEAK_CHECK}"
if [[ ! -f "$LEAK_CHECK_ABS" ]]; then
  die "$EX_MISSING_CHECK" "required local leak/identity check not found: $LEAK_CHECK_ABS
Run the full-tree public-leak gate before promoting:
    python ${LEAK_CHECK} --tree ${LEAK_TREE}
and confirm the CI identity-language-check delta gate is green on the PR."
fi
info "running leak/identity-surface scan: python ${LEAK_CHECK} --tree ${LEAK_TREE}"
LEAK_LOG="$(mktemp "${TMPDIR:-/tmp}/promote-leak-scan.XXXXXX")"
set +e
python3 "$LEAK_CHECK_ABS" --tree "${WORKTREE_DIR}/${LEAK_TREE}" >"$LEAK_LOG" 2>&1
leak_rc=$?
set -e
if [[ "$leak_rc" -eq 0 ]]; then
  ok "leak/identity-surface scan clean"
elif [[ "$leak_rc" -eq 1 ]]; then
  sed 's/^/    /' "$LEAK_LOG" >&2 || true
  die "$EX_LEAK" "leak/identity-surface scan FAILED on the prepared tree — forbidden public surface present; resolve before promoting (see CONTRIBUTING.md public language rules)"
else
  sed 's/^/    /' "$LEAK_LOG" >&2 || true
  die "$EX_MISSING_CHECK" "leak/identity check could not run (exit $leak_rc); run it manually: python ${LEAK_CHECK} --tree ${LEAK_TREE}"
fi

# ── Leave the prepared commit on a local branch for the human push ──────────
git branch -f "$PREPARED_BRANCH" "$CAND_SHA" >/dev/null 2>&1 \
  || die "$EX_SETUP" "could not record prepared branch $PREPARED_BRANCH"

# ── Moment-of-action: print the exact push command and STOP ─────────────────
info ""
info "════════════════════════════════════════════════════════════"
ok   "PREPARED + VERIFIED — ready for human approval."
info "  prepared SHA   : ${CAND_SHA}"
info "  re-anchored on : ${PUBLIC_SHA} (${PUBLIC_REF})"
info "  local branch   : ${PREPARED_BRANCH}"
info ""
info "  This script does NOT push. A public promotion requires separate approval"
info "  that must name this exact SHA. To land it (identity-clean fast-forward):"
info ""
info "      git push ${PUBLIC_REMOTE:-github} ${PREPARED_BRANCH}:${PUBLIC_BRANCH}"
info ""
info "  (Interim mechanic, equivalent: fast-forward local ${PUBLIC_BRANCH} to"
info "   ${CAND_SHA:0:12}, then  PROMOTE_PUBLIC_ALLOW=1 git push ${PUBLIC_REMOTE:-github} ${PUBLIC_BRANCH} )"
info ""
info "  RESULT: prepared ${CAND_SHA}"
info "════════════════════════════════════════════════════════════"
exit "$EX_OK"
