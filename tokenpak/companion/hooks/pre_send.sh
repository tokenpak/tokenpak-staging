#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# ──────────────────────────────────────────────────────────────
# Ultra-lean UserPromptSubmit hook — pure bash, ~30ms target.
#
# No python3 in the hot path. JSON fields extracted with grep.
# Budget check uses sqlite3 CLI (only if budget is set).
# ──────────────────────────────────────────────────────────────

# Read stdin
INPUT=$(cat)

# Quick exit if companion disabled
[ "${TOKENPAK_COMPANION_ENABLED:-1}" = "0" ] && exit 0

# Parse JSON fields — try jq first (fastest), fall back to sed (portable).
# A single jq pass extracts every field we need (transcript, session, prompt)
# so the 200k-byte payload is parsed once, not once per field. ``@sh``
# shell-quotes the values for safe ``eval`` (handles quotes/newlines/spaces).
PROMPT=""
if command -v jq >/dev/null 2>&1; then
    eval "$(echo "$INPUT" | jq -r '@sh "TRANSCRIPT=\(.transcript_path // "") SESSION_ID=\(.session_id // "") PROMPT=\(.prompt // "")"' 2>/dev/null)"
else
    # Portable sed extraction (no -P flag needed). PROMPT is jq-only (the
    # dynamic title is skipped without jq).
    TRANSCRIPT=$(echo "$INPUT" | sed -n 's/.*"transcript_path"\s*:\s*"\([^"]*\)".*/\1/p')
    SESSION_ID=$(echo "$INPUT" | sed -n 's/.*"session_id"\s*:\s*"\([^"]*\)".*/\1/p')
fi

# ── Native dynamic session title (compute only; emitted on allow paths) ─
# On the first prompt of a session, rename the Claude Code session to a
# short, prompt-derived title using Claude Code's NATIVE mechanism: the
# UserPromptSubmit ``hookSpecificOutput.sessionTitle`` field (documented as
# "same effect as /rename"). No OSC-0, no terminal escapes — Claude Code owns
# the title bar, session picker, and terminal title, and repaints on its own
# render loop (which is why manual OSC-0 never survived). Fires exactly once
# per session via a state marker; Claude Code's own aiTitle auto-naming may
# take over afterward. Requires jq so the emitted JSON is always well-formed
# — a malformed hook payload could disrupt prompt submission. Disable with
# TOKENPAK_COMPANION_DYNAMIC_TITLE=0.
#
# A UserPromptSubmit hook may emit at most one JSON object, so the title is
# computed here but only emitted (via emit_title) on the allow exits — never
# on the budget-block path, whose decision JSON takes precedence. The state
# marker is written only when the title is actually emitted, so a first
# prompt that gets blocked still earns its title on the next allow.
TITLE_JSON=""
TITLE_STATE_DIR=""
TITLE_STATE=""
if [ "${TOKENPAK_COMPANION_DYNAMIC_TITLE:-1}" != "0" ] \
   && [ -n "$SESSION_ID" ] \
   && [ -n "$PROMPT" ]; then
    TITLE_STATE_DIR="${TOKENPAK_COMPANION_JOURNAL_DIR:-$HOME/.tokenpak/companion}/titles"
    TITLE_STATE="$TITLE_STATE_DIR/$SESSION_ID"
    if [ ! -f "$TITLE_STATE" ]; then
        if [ -n "$PROMPT" ]; then
            # Sanitize: newlines/tabs → space, strip other control chars,
            # collapse runs of whitespace, trim, cap at 40 chars.
            SHORT=$(printf '%s' "$PROMPT" \
                | tr '\n\r\t' '   ' \
                | tr -d '\000-\037' \
                | sed -e 's/  */ /g' -e 's/^ //' -e 's/ *$//' \
                | cut -c1-40 \
                | sed -e 's/ *$//')
            if [ -n "$SHORT" ]; then
                # Build the payload with printf to avoid a second jq in the
                # hot path. SHORT is already control-char-free, so escaping
                # the only remaining JSON-significant bytes (backslash then
                # double-quote, in that order) yields a well-formed string.
                SHORT_ESC=$(printf '%s' "$SHORT" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g')
                TITLE_JSON=$(printf '{"hookSpecificOutput":{"hookEventName":"UserPromptSubmit","sessionTitle":"📦 %s"}}' "$SHORT_ESC")
            fi
        fi
    fi
fi

# emit_title — print the one-time native rename payload on an allow exit and
# mark the session so it fires exactly once. No-op when no title is pending.
emit_title() {
    [ -n "$TITLE_JSON" ] || return 0
    mkdir -p "$TITLE_STATE_DIR" 2>/dev/null
    : > "$TITLE_STATE" 2>/dev/null
    printf '%s\n' "$TITLE_JSON"
}

# Token estimation from file size (instant via stat)
TOKENS=0
if [ -n "$TRANSCRIPT" ] && [ -f "$TRANSCRIPT" ]; then
    FILE_SIZE=$(stat -c%s "$TRANSCRIPT" 2>/dev/null || stat -f%z "$TRANSCRIPT" 2>/dev/null || echo 0)
    TOKENS=$((FILE_SIZE / 4))
fi

[ "$TOKENS" -eq 0 ] && { emit_title; exit 0; }

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
    JOURNAL_DIR="${TOKENPAK_COMPANION_JOURNAL_DIR:-$HOME/.tokenpak/companion}"
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

# Allow path: emit the one-time native session rename (no-op if none pending).
emit_title
exit 0
