#!/usr/bin/env bash
# session_start_name.sh — TokenPak Companion SessionStart hook.
#
# Claude Code fires SessionStart on session-creation events (startup,
# clear, resume, compact). The launcher passes the branded label via
# ``--name`` at startup, but ``--name`` is per-session: a /clear creates
# a new session that inherits no name, so the top-HR chat-header reverts
# to default chrome.
#
# This hook re-asserts the branded label by emitting
# ``hookSpecificOutput.sessionTitle``. Real ESC bytes are invalid in
# JSON strings, so the ANSI escapes are emitted as ``\u001b`` literals —
# the consumer's JSON parser decodes them back to ESC.
#
# Matcher: registered for ``"startup|clear|resume|compact"`` by the
# launcher so every session re-creation event re-asserts the label.

# Branded session label — must stay in sync with
# ``tokenpak/companion/launcher.py::_DEFAULT_SESSION_LABEL``.
#
# Two-tone: tiffany brackets on chrome BG (default), inner span on black
# BG pill so the tiffany "Pak" glyph stops vanishing into the tiffany
# chat-header chrome.
#
#   tiffany FG / default BG (brackets) = \u001b[38;2;0;180;170m
#   tiffany FG / black BG   ("Pak")    = \u001b[38;2;0;180;170;48;2;0;0;0m
#   white   FG / black BG ("📦 Token") = \u001b[38;2;255;255;255;48;2;0;0;0m
#   gray    FG / black BG (Companion)  = \u001b[38;2;90;94;105;48;2;0;0;0m
#   black BG only (spacer)             = \u001b[48;2;0;0;0m
#   reset                              = \u001b[0m

cat <<'JSON'
{
  "hookSpecificOutput": {
    "hookEventName": "SessionStart",
    "sessionTitle": "\u001b[38;2;0;180;170m[\u001b[48;2;0;0;0m \u001b[38;2;255;255;255;48;2;0;0;0m📦 Token\u001b[38;2;0;180;170;48;2;0;0;0mPak\u001b[38;2;90;94;105;48;2;0;0;0m Claude Companion\u001b[48;2;0;0;0m \u001b[0m\u001b[38;2;0;180;170m]\u001b[0m"
  }
}
JSON
