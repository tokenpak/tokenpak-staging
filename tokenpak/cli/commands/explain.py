# SPDX-License-Identifier: Apache-2.0
"""``tokenpak status --explain`` — per-request savings / skip explanation.

Unified ``--explain`` flag (RULED 2026-05-31): with a request id, explain why
that request saved what it did, surfacing per-request drop/skip reasons in
human-readable form (Constitution §8 transparency; ``unknown`` is a valid value
per §5.3). With no id, route to value-tier confidence notes (Item A's surface).

Per-request optimization-trace ``skip_reason`` is opt-in (``emit_trace``) and is
not persisted to ``monitor.db``, so when no explicit drop reason is recorded for
a request this command renders ``unknown`` rather than inventing a schema
migration (the ratified-C gap2 boundary).
"""
from __future__ import annotations

import sqlite3
from typing import Optional

# Sentinel that argparse stores via `const=` when --explain is passed with no
# request id. Kept in sync with the parser registration in _cli_core.py.
NO_ARG = "__NOARG__"

# Columns that, when present in a request row, may carry an explicit drop reason.
_SKIP_KEYS = ("drop_reason", "drop_reasons", "skip_reason", "request_class")


def _open() -> Optional[sqlite3.Connection]:
    """Open monitor.db read-only-ish with a Row factory, or None if absent."""
    try:
        from tokenpak._paths import monitor_db

        path = monitor_db(mode="read")
    except Exception:
        path = None
    if not path:
        return None
    try:
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


def _g(d: dict, *keys):
    """First present, non-empty value among keys (graceful across schema variants)."""
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


def explain_request(req_id) -> int:
    """Render a single request's explanation. Returns a process exit code."""
    try:
        rid = int(str(req_id).strip())
    except (TypeError, ValueError):
        print(f"⚠️  --explain expects a numeric request id (got {req_id!r}).")
        print("   List recent ids with: tokenpak status --json")
        return 2

    conn = _open()
    if conn is None:
        print("⚠️  No monitor.db found yet — nothing to explain.")
        return 1
    try:
        row = conn.execute("SELECT * FROM requests WHERE id = ?", (rid,)).fetchone()
    except sqlite3.Error as exc:
        print(f"⚠️  Could not read request {rid}: {exc}")
        return 1
    finally:
        conn.close()

    if row is None:
        print(f"No request found with id {rid}. List ids with: tokenpak status --json")
        return 1

    _render(rid, {k: row[k] for k in row.keys()})
    return 0


def _render(rid: int, d: dict) -> None:
    print(f"Request #{rid} — explanation")

    header = [str(v) for v in (_g(d, "timestamp"), _g(d, "model")) if v is not None]
    status = _g(d, "status_code")
    if status is not None:
        header.append(f"HTTP {status}")
    if header:
        print("  " + "  ·  ".join(header))

    it, ot = _g(d, "input_tokens"), _g(d, "output_tokens")
    if it is not None or ot is not None:
        print(f"  Tokens:           in={it if it is not None else '?'}  out={ot if ot is not None else '?'}")

    mode = _g(d, "compression_mode", "compilation_mode")
    comp = _g(d, "compressed_tokens")
    print(f"  Compression:      mode={mode if mode is not None else 'unknown'}  compressed_tokens={comp if comp is not None else 0}")

    inj, inj_src = _g(d, "injected_tokens"), _g(d, "injected_sources")
    if inj is not None and int(inj or 0) > 0:
        print(f"  Injection:        {inj} tokens" + (f" from {inj_src}" if inj_src else ""))

    cache_r, cache_o = _g(d, "cache_read_tokens"), _g(d, "cache_origin")
    if cache_r is not None and int(cache_r or 0) > 0:
        print(f"  Cache:            read {cache_r} tokens" + (f" (origin={cache_o})" if cache_o else ""))

    whs = _g(d, "would_have_saved")
    if whs is not None:
        print(f"  Would-have-saved: {whs}")

    # Per-request drop/skip reason: surfaced if recorded, else honest unknown.
    reason = _g(d, *_SKIP_KEYS)
    if reason is None:
        reason = "unknown (per-request optimization trace not persisted; enable TIP trace emission to populate)"
    print(f"  Drop reasons:     {reason}")

    notes = []
    if comp in (None, 0, "0") and mode in (None, "", "off", "none", "disabled"):
        notes.append("no compression applied for this request")
    if cache_r in (None, 0, "0"):
        notes.append("no proxy cache read")
    if not notes:
        notes.append("savings recorded from the fields above")
    print("  Summary:          " + "; ".join(notes) + ".")


def render_value_tier_notes() -> int:
    """``--explain`` with no request id → value-tier confidence notes (Item A).

    Item A (value-report confidence tiers) owns this surface; until it lands this
    prints an honest pointer rather than erroring, so the unified flag works now.
    """
    print("Value confidence tiers (`tokenpak status --explain`):")
    print("  confirmed  — provider-confirmed savings + exact avoided upstream cost")
    print("  estimated  — TokenPak-modeled value (compression / routing / cache reuse)")
    print("  unpriced   — token / request / latency efficiency without a defensible $ value")
    print("Pass a request id (`--explain <req_id>`) to explain a single request.")
    return 0


def run_explain(value) -> int:
    """Unified --explain dispatcher (RULED 2026-05-31).

    ``value`` is the argparse result: the :data:`NO_ARG` sentinel when --explain
    was passed with no argument (→ value-tier notes), otherwise a request id
    (→ per-request explanation).
    """
    if value == NO_ARG or value is None:
        return render_value_tier_notes()
    return explain_request(value)
