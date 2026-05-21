#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# ──────────────────────────────────────────────────────────────
# Ultra-lean UserPromptSubmit hook — pure bash, ~30ms target.
#
# No python3 in the hot path. JSON fields extracted with grep.
# Budget check uses sqlite3 CLI (only if budget is set).
#
# Also drives the dynamic terminal-tab title: derives a short
# title from the first user prompt of a session, persists it to
# a per-session state file, then re-asserts it via OSC 0 on every
# subsequent prompt (so Claude Code's own rewrites don't reclaim
# the tab). Disable with TOKENPAK_COMPANION_DYNAMIC_TITLE=0.
# ──────────────────────────────────────────────────────────────

# Read stdin
INPUT=$(cat)

# Quick exit if companion disabled
[ "${TOKENPAK_COMPANION_ENABLED:-1}" = "0" ] && exit 0

# Parse JSON fields — try jq first (fastest), fall back to sed (portable)
if command -v jq >/dev/null 2>&1; then
    TRANSCRIPT=$(echo "$INPUT" | jq -r '.transcript_path // empty' 2>/dev/null)
    SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // empty' 2>/dev/null)
    PROMPT=$(echo "$INPUT" | jq -r '.prompt // empty' 2>/dev/null)
else
    # Portable sed extraction (no -P flag needed)
    TRANSCRIPT=$(echo "$INPUT" | sed -n 's/.*"transcript_path"\s*:\s*"\([^"]*\)".*/\1/p')
    SESSION_ID=$(echo "$INPUT" | sed -n 's/.*"session_id"\s*:\s*"\([^"]*\)".*/\1/p')
    PROMPT=$(echo "$INPUT" | sed -n 's/.*"prompt"\s*:\s*"\([^"]*\)".*/\1/p')
fi

JOURNAL_DIR="${TOKENPAK_COMPANION_JOURNAL_DIR:-$HOME/.tokenpak/companion}"

# ──────────────────────────────────────────────────────────────
# Dynamic terminal-tab title
# ──────────────────────────────────────────────────────────────
# First prompt of a session → derive title from prompt, persist.
# Every subsequent prompt → re-emit so Claude Code's tab rewrites
# don't reclaim the tab back to its own default.
if [ -n "$SESSION_ID" ] && [ "${TOKENPAK_COMPANION_DYNAMIC_TITLE:-1}" != "0" ]; then
    TITLE_FILE="$JOURNAL_DIR/title-${SESSION_ID}.txt"
    if [ ! -s "$TITLE_FILE" ] && [ -n "$PROMPT" ]; then
        mkdir -p "$JOURNAL_DIR" 2>/dev/null
        # First non-empty line, trim, truncate to 40 chars, strip trailing space
        DERIVED=$(printf '%s' "$PROMPT" \
            | head -1 \
            | tr -d '\r\n\t' \
            | cut -c1-40 \
            | sed 's/[[:space:]]*$//')
        [ -n "$DERIVED" ] && printf '%s' "$DERIVED" > "$TITLE_FILE"
    fi
    if [ -s "$TITLE_FILE" ]; then
        TITLE=$(cat "$TITLE_FILE")
        # OSC 0: set both icon-name and window-title. \xf0\x9f\x93\xa6 = 📦
        printf '\033]0;\xf0\x9f\x93\xa6 %s\007' "$TITLE" >&2
    fi
fi

# Token estimation from file size (instant via stat)
TOKENS=0
if [ -n "$TRANSCRIPT" ] && [ -f "$TRANSCRIPT" ]; then
    FILE_SIZE=$(stat -c%s "$TRANSCRIPT" 2>/dev/null || stat -f%z "$TRANSCRIPT" 2>/dev/null || echo 0)
    TOKENS=$((FILE_SIZE / 4))
fi

[ "$TOKENS" -eq 0 ] && exit 0

# Format token count with thousands separators (pure bash)
TOKENS_FMT=$(printf '%d' "$TOKENS" | rev | sed 's/.\{3\}/&,/g' | rev | sed 's/^,//')

# Cost estimation (sonnet rate: $3/M tokens)
# Integer math in microdollars to avoid float
COST_MICRO=$((TOKENS * 3 / 1000))
COST_DOLLARS="$(( COST_MICRO / 1000 )).$(printf '%04d' $(( COST_MICRO % 1000 )) )"

# Budget check (only if TOKENPAK_COMPANION_BUDGET is set and > 0)
BUDGET="${TOKENPAK_COMPANION_BUDGET:-0}"
BUDGET_TAG=""

if [ "$BUDGET" != "0" ] && [ -n "$BUDGET" ]; then
    BUDGET_DB="$JOURNAL_DIR/budget.db"
    TODAY=$(date +%Y-%m-%d)
    DAILY_TOTAL="0.0"

    if [ -f "$BUDGET_DB" ] && command -v sqlite3 >/dev/null 2>&1; then
        DAILY_TOTAL=$(sqlite3 "$BUDGET_DB" \
            "SELECT COALESCE(SUM(estimated_cost), 0) FROM companion_costs WHERE date = '$TODAY';" 2>/dev/null || echo "0.0")
    fi

    # Compare using integer microdollars
    BUDGET_MICRO=$(echo "$BUDGET * 1000000" | bc 2>/dev/null | cut -d. -f1 || echo 0)
    DAILY_MICRO=$(echo "$DAILY_TOTAL * 1000000" | bc 2>/dev/null | cut -d. -f1 || echo 0)
    EST_MICRO=$((TOKENS * 3))

    if [ "$((DAILY_MICRO + EST_MICRO))" -gt "${BUDGET_MICRO:-0}" ] 2>/dev/null; then
        echo "tokenpak: budget exceeded (\$$DAILY_TOTAL / \$$BUDGET daily)" >&2
        printf '{"hookSpecificOutput":{"hookEventName":"UserPromptSubmit","decision":"block","reason":"budget exceeded"}}\n'
        exit 2
    fi

    # Budget percentage tag
    if [ "${BUDGET_MICRO:-0}" -gt 0 ] 2>/dev/null; then
        PCT=$((DAILY_MICRO * 100 / BUDGET_MICRO))
        [ "$PCT" -gt 50 ] && BUDGET_TAG="  budget ${PCT}%"
    fi
fi

# Print cost estimate to stderr (visible in TUI)
if [ "${TOKENPAK_COMPANION_SHOW_COST:-1}" != "0" ]; then
    printf 'tokenpak: ~%s tokens  est $%s%s\n' "$TOKENS_FMT" "$COST_DOLLARS" "$BUDGET_TAG" >&2
fi

exit 0
