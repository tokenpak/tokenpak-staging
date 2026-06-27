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
    eval "$(printf '%s' "$INPUT" | jq -r '@sh "TRANSCRIPT=\(.transcript_path // "") SESSION_ID=\(.session_id // "") PROMPT=\(.prompt // "")"' 2>/dev/null)"
else
    # Portable sed extraction (no -P flag needed). PROMPT is jq-only (the
    # dynamic title is skipped without jq).
    TRANSCRIPT=$(printf '%s' "$INPUT" | sed -n 's/.*"transcript_path"\s*:\s*"\([^"]*\)".*/\1/p')
    SESSION_ID=$(printf '%s' "$INPUT" | sed -n 's/.*"session_id"\s*:\s*"\([^"]*\)".*/\1/p')
fi

if [ -n "$SESSION_ID" ]; then
    JOURNAL_DIR="${TOKENPAK_COMPANION_JOURNAL_DIR:-$HOME/.tokenpak/companion}"
    RUN_DIR="$JOURNAL_DIR/run"
    if [ -d "$RUN_DIR" ] || mkdir -p "$RUN_DIR" 2>/dev/null; then
        CURRENT_SESSION_FILE="$RUN_DIR/current-session"
        OLD_SESSION_ID=""
        if [ -f "$CURRENT_SESSION_FILE" ]; then
            IFS= read -r OLD_SESSION_ID < "$CURRENT_SESSION_FILE" || true
        fi
        if [ "$OLD_SESSION_ID" != "$SESSION_ID" ]; then
            printf '%s\n' "$SESSION_ID" > "$CURRENT_SESSION_FILE" 2>/dev/null || true
        fi
    fi
fi

# derive_title — turn a raw prompt into a short SEMANTIC task title (not a
# prefix truncation). Strips boilerplate openers, detects a leading task verb
# → task noun, keeps the first significant topic words, recases known tokens,
# caps at ~40 chars on a word boundary. Pure-bash word processing + 2 external
# calls (tr, one sed) to stay within the hook budget. Returns non-zero (no
# output) when nothing meaningful survives, so the caller can fall back.
derive_title() {
    local raw="$1" core verb="" out="" outlen=0 w n=0 lim=4 pretty add changed
    # Cleanup is PURE BASH (zero subshells, ~3ms): lowercase, newlines/tabs →
    # space, drop apostrophes, keep first sentence, collapse/trim, then
    # loop-strip boilerplate openers + leading articles. (Exotic control chars
    # are stripped JSON-side by the caller's escape pass.)
    core="${raw,,}"
    core="${core//$'\n'/ }"; core="${core//$'\t'/ }"; core="${core//$'\r'/ }"
    core="${core//\'/}"
    core="${core%%[.?!]*}"
    while [[ "$core" == *"  "* ]]; do core="${core//  / }"; done
    core="${core# }"; core="${core% }"
    core="${core//the following /}"
    changed=1
    while [ "$changed" = 1 ]; do
        changed=0
        case "$core" in
            "please "*) core="${core#please }"; changed=1 ;;
            "kindly "*) core="${core#kindly }"; changed=1 ;;
            "just "*) core="${core#just }"; changed=1 ;;
            "simply "*) core="${core#simply }"; changed=1 ;;
            "so "*) core="${core#so }"; changed=1 ;;
            "ok "*) core="${core#ok }"; changed=1 ;;
            "okay "*) core="${core#okay }"; changed=1 ;;
            "hey "*|"hi "*|"well "*|"now "*) core="${core#* }"; changed=1 ;;
            "can you "*) core="${core#can you }"; changed=1 ;;
            "could you "*) core="${core#could you }"; changed=1 ;;
            "would you "*) core="${core#would you }"; changed=1 ;;
            "will you "*) core="${core#will you }"; changed=1 ;;
            "id like you to "*) core="${core#id like you to }"; changed=1 ;;
            "id would like you to "*) core="${core#id would like you to }"; changed=1 ;;
            "i would like to "*) core="${core#i would like to }"; changed=1 ;;
            "i want you to "*) core="${core#i want you to }"; changed=1 ;;
            "i need you to "*) core="${core#i need you to }"; changed=1 ;;
            "help me to "*) core="${core#help me to }"; changed=1 ;;
            "help me "*) core="${core#help me }"; changed=1 ;;
            "lets "*) core="${core#lets }"; changed=1 ;;
            "here is "*) core="${core#here is }"; changed=1 ;;
            "here are "*) core="${core#here are }"; changed=1 ;;
            "heres "*) core="${core#heres }"; changed=1 ;;
            "give me "*) core="${core#give me }"; changed=1 ;;
            "get me "*) core="${core#get me }"; changed=1 ;;
            "i need "*) core="${core#i need }"; changed=1 ;;
            "i want "*) core="${core#i want }"; changed=1 ;;
            "take a look at "*) core="${core#take a look at }"; changed=1 ;;
            "look at "*) core="${core#look at }"; changed=1 ;;
            "consider "*) core="${core#consider }"; changed=1 ;;
            "what do you think about "*) core="${core#what do you think about }"; changed=1 ;;
            "what do you think of "*) core="${core#what do you think of }"; changed=1 ;;
            "what do you think "*) core="${core#what do you think }"; changed=1 ;;
            "i believe "*) core="${core#i believe }"; changed=1 ;;
            "i think "*) core="${core#i think }"; changed=1 ;;
            "i feel like "*) core="${core#i feel like }"; changed=1 ;;
            "i feel "*) core="${core#i feel }"; changed=1 ;;
            "i guess "*) core="${core#i guess }"; changed=1 ;;
            "we cant "*) core="${core#we cant }"; changed=1 ;;
            "we cannot "*) core="${core#we cannot }"; changed=1 ;;
            "we could not "*) core="${core#we could not }"; changed=1 ;;
            "we need to "*) core="${core#we need to }"; changed=1 ;;
            "why cant "*) core="${core#why cant }"; changed=1 ;;
            "why not "*) core="${core#why not }"; changed=1 ;;
            "the "*) core="${core#the }"; changed=1 ;;
            "an "*) core="${core#an }"; changed=1 ;;
            "a "*) core="${core#a }"; changed=1 ;;
            "this "*|"that "*|"these "*|"those "*) core="${core#* }"; changed=1 ;;
            "our "*|"your "*|"my "*|"its "*) core="${core#* }"; changed=1 ;;
        esac
    done
    case "$core" in
        analyze\ *)     verb="analysis";       core="${core#analyze }" ;;
        analyse\ *)     verb="analysis";       core="${core#analyse }" ;;
        review\ *)      verb="review";         core="${core#review }" ;;
        fix\ *)         verb="fix";            core="${core#fix }" ;;
        debug\ *)       verb="fix";            core="${core#debug }" ;;
        implement\ *)   verb="implementation"; core="${core#implement }" ;;
        draft\ *)       verb="draft";          core="${core#draft }" ;;
        write\ *)       verb="draft";          core="${core#write }" ;;
        compare\ *)     verb="comparison";     core="${core#compare }" ;;
        investigate\ *) verb="investigation";  core="${core#investigate }" ;;
        refactor\ *)    verb="refactor";       core="${core#refactor }" ;;
        design\ *)      verb="design";         core="${core#design }" ;;
        update\ *)      verb="update";         core="${core#update }" ;;
        revise\ *)      verb="revision";       core="${core#revise }" ;;
        explain\ *)     verb="explanation";    core="${core#explain }" ;;
        create\ *)      verb="setup";          core="${core#create }" ;;
        add\ *)         verb="setup";          core="${core#add }" ;;
    esac
    [ -n "$verb" ] && lim=3
    for w in $core; do
        case "$w" in
            the|a|an|this|that|these|those|for|to|of|on|in|at|by|with|and|or|but|is|are|was|were|be|been|being|do|not|it|i|we|you|they|them|our|your|my|set|just|response|from|about|into|make|made|get|as|so|if|then|following|some|any|more|updated|new) continue ;;
        esac
        case "$w" in
            pakline) pretty="PakLine" ;; tokenpak) pretty="TokenPak" ;;
            ci) pretty="CI" ;; pr) pretty="PR" ;; api) pretty="API" ;;
            json) pretty="JSON" ;; tui) pretty="TUI" ;; mcp) pretty="MCP" ;;
            ttl) pretty="TTL" ;; osc) pretty="OSC" ;; tip) pretty="TIP" ;;
            pak) pretty="PAK" ;; oauth) pretty="OAuth" ;;
            *) pretty="$w" ;;
        esac
        add=$(( ${#pretty} + 1 ))
        [ $(( outlen + add )) -gt 40 ] && break
        out="$out $pretty"; outlen=$(( outlen + add )); n=$(( n + 1 ))
        [ "$n" -ge "$lim" ] && break
    done
    out="${out# }"
    if [ -n "$verb" ] && [ -n "$out" ] && [ $(( outlen + ${#verb} + 1 )) -le 40 ]; then
        out="$out $verb"
    fi
    [ -n "$out" ] || return 1
    printf '%s' "${out^}"
}

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
TITLE_TEXT=""
TITLE_STATE_DIR=""
TITLE_STATE=""
if [ "${TOKENPAK_COMPANION_DYNAMIC_TITLE:-1}" != "0" ] \
   && [ -n "$SESSION_ID" ] \
   && [ -n "$PROMPT" ]; then
    TITLE_STATE_DIR="${TOKENPAK_COMPANION_JOURNAL_DIR:-$HOME/.tokenpak/companion}/titles"
    TITLE_STATE="$TITLE_STATE_DIR/$SESSION_ID"
    if [ ! -f "$TITLE_STATE" ]; then
        if [ -n "$PROMPT" ]; then
            # Semantic title (verb/topic phrase). Fall back to a cleaned
            # leading phrase only if the heuristic yields nothing usable.
            SHORT=$(derive_title "$PROMPT") || SHORT=""
            if [ -z "$SHORT" ]; then
                SHORT=$(printf '%s' "$PROMPT" \
                    | tr '\n\r\t' '   ' \
                    | tr -d '\000-\037' \
                    | sed -e 's/  */ /g' -e 's/^ //' -e 's/ *$//' \
                    | cut -c1-40 \
                    | sed -E 's/ +[^ ]*$//')
            fi
            if [ -n "$SHORT" ]; then
                # Build the payload with printf to avoid a second jq in the
                # hot path. SHORT is already control-char-free, so escaping
                # the only remaining JSON-significant bytes (backslash then
                # double-quote, in that order) yields a well-formed string.
                SHORT_ESC=$(printf '%s' "$SHORT" | sed -e 's/[[:cntrl:]]//g' -e 's/\\/\\\\/g' -e 's/"/\\"/g')
                TITLE_JSON=$(printf '{"hookSpecificOutput":{"hookEventName":"UserPromptSubmit","sessionTitle":"📦 %s"}}' "$SHORT_ESC")
                TITLE_TEXT="$SHORT"
            fi
        fi
    fi
fi

# emit_title — print the one-time native rename payload on an allow exit and
# mark the session so it fires exactly once. No-op when no title is pending.
emit_title() {
    [ -n "$TITLE_JSON" ] || return 0
    mkdir -p "$TITLE_STATE_DIR" 2>/dev/null
    # Store the title TEXT (not an empty marker): it doubles as the fire-once
    # flag AND PakLine's O(1) task source ($JOURNAL_DIR/titles/<session_id>).
    printf '%s\n' "$TITLE_TEXT" > "$TITLE_STATE" 2>/dev/null
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
TOKENS_FMT="$TOKENS"
if [ "$TOKENS" -ge 1000 ]; then
    TOKENS_FMT=""
    REST="$TOKENS"
    while [ "${#REST}" -gt 3 ]; do
        TOKENS_FMT=",${REST: -3}$TOKENS_FMT"
        REST="${REST:0:${#REST}-3}"
    done
    TOKENS_FMT="$REST$TOKENS_FMT"
fi

# Cost estimation (sonnet rate: $3/M tokens)
# Integer math in microdollars to avoid float
COST_MICRO=$((TOKENS * 3 / 1000))
COST_DOLLARS="$(( COST_MICRO / 1000 )).$(printf '%04d' $(( COST_MICRO % 1000 )) )"

# ── Budget ledger backend (sqlite3 CLI or bundled Python) ─────────────────
# The companion budget ledger (companion_costs) is a SQLite DB. bash can only
# reach it through the host `sqlite3` CLI, which is not installed on every
# host — when it is absent the gate could neither read accumulated spend nor
# record this prompt, so the budget never accumulated and the gate was inert.
# These helpers prefer the CLI when present and fall back to the bundled Python
# sqlite3 module otherwise, so enforcement works without a host CLI dependency.
# Pinned interpreter (TOKENPAK_COMPANION_PYTHON, set by the launcher) keeps the
# fallback on the same venv interpreter as the MCP server.
_companion_daily_total() {
    # $1=db $2=date → today's SUM(estimated_cost); prints 0.0 on any error.
    local out
    if [ -f "$1" ] && command -v sqlite3 >/dev/null 2>&1; then
        out=$(sqlite3 "$1" \
            "SELECT COALESCE(SUM(estimated_cost), 0) FROM companion_costs WHERE date = '$2';" \
            2>/dev/null)
        # A working CLI always returns a value (COALESCE → at least 0). Empty
        # output means the CLI failed; fall through to the bundled Python path
        # rather than silently reading 0 and failing the gate open.
        if [ -n "$out" ]; then printf '%s' "$out"; return; fi
    fi
    "${TOKENPAK_COMPANION_PYTHON:-python3}" -c '
import os, sqlite3, sys
db, day = sys.argv[1], sys.argv[2]
if not os.path.exists(db):
    print(0.0)
else:
    try:
        conn = sqlite3.connect(db)
        row = conn.execute(
            "SELECT COALESCE(SUM(estimated_cost), 0) FROM companion_costs WHERE date = ?",
            (day,),
        ).fetchone()
        conn.close()
        print(row[0] if row else 0.0)
    except Exception:
        print(0.0)
' "$1" "$2" 2>/dev/null || printf '0.0'
}

_companion_record_cost() {
    # $1=db $2=date $3=session_id $4=input_tokens $5=est_microdollars.
    # Records the pre-send estimate so the daily total accumulates (the basis
    # for the gate). Bundled Python sqlite3 — no host CLI dependency.
    # Best-effort; never fails the hook. All columns are supplied on INSERT, so
    # the table is created without per-column DEFAULTs (schema-compatible with
    # companion/budget/tracker.py, which also uses CREATE TABLE IF NOT EXISTS).
    "${TOKENPAK_COMPANION_PYTHON:-python3}" -c '
import os, sqlite3, sys, time
db, day, sid = sys.argv[1], sys.argv[2], sys.argv[3]
toks, micro = int(sys.argv[4]), int(sys.argv[5])
try:
    d = os.path.dirname(db)
    if d:
        os.makedirs(d, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS companion_costs ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp REAL NOT NULL, "
        "date TEXT NOT NULL, session_id TEXT NOT NULL, model TEXT NOT NULL, "
        "input_tokens INTEGER NOT NULL, cached_tokens INTEGER NOT NULL, "
        "output_tokens INTEGER NOT NULL, estimated_cost REAL NOT NULL)"
    )
    conn.execute(
        "INSERT INTO companion_costs (timestamp, date, session_id, model, "
        "input_tokens, cached_tokens, output_tokens, estimated_cost) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (time.time(), day, sid, "", toks, 0, 0, round(micro / 1_000_000, 6)),
    )
    conn.commit()
    conn.close()
except Exception:
    pass
' "$1" "$2" "$3" "$4" "$5" 2>/dev/null || true
}

# Budget check (only if TOKENPAK_COMPANION_BUDGET is set and > 0)
BUDGET="${TOKENPAK_COMPANION_BUDGET:-0}"
BUDGET_TAG=""

if [ "$BUDGET" != "0" ] && [ -n "$BUDGET" ]; then
    JOURNAL_DIR="${TOKENPAK_COMPANION_JOURNAL_DIR:-$HOME/.tokenpak/companion}"
    BUDGET_DB="$JOURNAL_DIR/budget.db"
    TODAY=$(date +%Y-%m-%d)

    # Accumulated spend today — reads via sqlite3 CLI or bundled Python so the
    # gate sees real accumulated spend even on hosts without the sqlite3 CLI.
    DAILY_TOTAL=$(_companion_daily_total "$BUDGET_DB" "$TODAY")

    # Compare using integer microdollars
    BUDGET_MICRO=$(echo "$BUDGET * 1000000" | bc 2>/dev/null | cut -d. -f1 || echo 0)
    DAILY_MICRO=$(echo "$DAILY_TOTAL * 1000000" | bc 2>/dev/null | cut -d. -f1 || echo 0)
    EST_MICRO=$((TOKENS * 3))

    if [ "$((DAILY_MICRO + EST_MICRO))" -gt "${BUDGET_MICRO:-0}" ] 2>/dev/null; then
        echo "tokenpak: budget exceeded (\$$DAILY_TOTAL / \$$BUDGET daily)" >&2
        printf '{"hookSpecificOutput":{"hookEventName":"UserPromptSubmit","decision":"block","reason":"budget exceeded"}}\n'
        exit 2
    fi

    # Allowed: record this prompt's estimate so the daily total accumulates
    # across prompts (otherwise the gate above could never read prior spend and
    # would be inert). Recorded after the gate read, so no double-count.
    _companion_record_cost "$BUDGET_DB" "$TODAY" "$SESSION_ID" "$TOKENS" "$EST_MICRO"

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
