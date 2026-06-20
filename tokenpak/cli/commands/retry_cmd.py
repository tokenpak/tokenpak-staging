# SPDX-License-Identifier: Apache-2.0
"""CLI handlers for ``tokenpak retry`` subcommands.

Commands
--------
``tokenpak retry drain``
    List and clear drainable pending recovery records.  Records that require
    a visible continuation turn are skipped unless ``--visible-turn`` is
    passed.  Deterministic failures are never drained (they must be
    cleared manually or by the user).

``tokenpak codex continue --last-failed``
    Build a new visible continuation turn from the most recently failed
    upstream request record.  Never silently replays partial output.
"""

from __future__ import annotations

import argparse
import json
from typing import List, Optional

# ── retry drain ────────────────────────────────────────────────────────────

def cmd_retry_drain(args: argparse.Namespace) -> int:
    """Handler for ``tokenpak retry drain``."""
    from tokenpak.proxy.upstream_retry import (
        delete_record_file,
        list_record_files,
    )

    visible_turn = getattr(args, "visible_turn", False)
    json_output = getattr(args, "json", False)
    dry_run = getattr(args, "dry_run", False)

    items = list_record_files()
    if not items:
        if json_output:
            print(json.dumps({"status": "empty", "drained": 0, "skipped": 0}))
        else:
            print("No pending recovery records.")
        return 0

    drained: List[dict] = []
    skipped: List[dict] = []

    for path, record in items:
        summary = {
            "request_id": record.request_id,
            "tip_plan_id": record.tip_plan_id,
            "terminal_recovery_status": record.terminal_recovery_status,
            "visible_continuation_required": record.visible_continuation_required,
            "stream_started": record.stream_started,
            "created_at": record.created_at,
        }

        if record.is_deterministic_failure():
            summary["skip_reason"] = "deterministic_failure"
            skipped.append(summary)
            continue

        if record.visible_continuation_required and not visible_turn:
            summary["skip_reason"] = "requires_visible_turn"
            skipped.append(summary)
            continue

        # Drainable — process and remove
        if not dry_run:
            delete_record_file(path)
        summary["dry_run"] = dry_run
        drained.append(summary)

    if json_output:
        print(json.dumps(
            {"status": "done", "drained": len(drained), "skipped": len(skipped),
             "records": {"drained": drained, "skipped": skipped}},
            indent=2,
        ))
        return 0

    # Human-readable output
    if drained:
        print(f"Drained {len(drained)} record(s){' (dry-run)' if dry_run else ''}:")
        for r in drained:
            print(f"  request_id={r['request_id']}  plan={r.get('tip_plan_id') or 'n/a'}")
    if skipped:
        print(f"Skipped {len(skipped)} record(s):")
        for r in skipped:
            print(
                f"  request_id={r['request_id']}  reason={r['skip_reason']}"
                f"  plan={r.get('tip_plan_id') or 'n/a'}"
            )
    if not drained and not skipped:
        print("No records processed.")
    return 0


# ── codex continue --last-failed ──────────────────────────────────────────

def cmd_codex_continue_last_failed(sub_argv: Optional[List[str]] = None) -> int:
    """Handler for ``tokenpak codex continue --last-failed``.

    Reads the most recent failed upstream retry record and builds a new
    visible continuation turn.  Never silently replays; always surfaces
    context for the user to act on.
    """
    from tokenpak.proxy.upstream_retry import most_recent_failed

    p = argparse.ArgumentParser(
        prog="tokenpak codex continue",
        description="Build a visible continuation turn from the last failed request.",
    )
    p.add_argument(
        "--last-failed",
        action="store_true",
        required=True,
        help="Select the most recent failed upstream retry record",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Emit recovery context as JSON instead of human text",
    )
    args = p.parse_args(sub_argv or [])

    result = most_recent_failed()
    if result is None:
        if args.json:
            print(json.dumps({"status": "no_records"}))
        else:
            print("No recovery records found in recovery/upstream/.")
            print("Nothing to continue.")
        return 1

    path, record = result

    if args.json:
        print(json.dumps(record.safe_dict(), indent=2))
        return 0

    # Build a human-visible continuation prompt — NOT hidden replay.
    lines = [
        "── Upstream retry recovery ─────────────────────────────────────",
        f"  request_id  : {record.request_id}",
        f"  tip_plan_id : {record.tip_plan_id or 'n/a'}",
        f"  endpoint    : {record.endpoint}",
        f"  provider    : {record.provider or 'n/a'}",
        f"  model       : {record.model or 'n/a'}",
        f"  stream_started          : {record.stream_started}",
        f"  visible_continuation    : {record.visible_continuation_required}",
        f"  terminal_recovery_status: {record.terminal_recovery_status}",
        f"  created_at  : {record.created_at}",
        "",
    ]

    if record.headers_redacted:
        lines.append("  headers (credential values redacted):")
        for k, v in sorted(record.headers_redacted.items()):
            lines.append(f"    {k}: {v}")
        lines.append("")

    if record.body_hash:
        lines.append(f"  body_hash   : {record.body_hash}")
    if record.body_preview:
        preview = record.body_preview.replace("\n", "↵")[:120]
        lines.append(f"  body_preview: {preview}")
        if record.body_persisted:
            lines.append("  body_full   : available (TOKENPAK_RETRY_PERSIST_BODY was set)")
        else:
            lines.append(
                "  body_full   : NOT available — re-run with "
                "TOKENPAK_RETRY_PERSIST_BODY=1 to capture full body on next failure"
            )
    else:
        lines.append("  body_preview: (empty — no body was sent with original request)")

    lines += [
        "",
        "── Next steps ───────────────────────────────────────────────────",
        "  This is a NEW visible turn, not a hidden replay.",
        "  Review the context above and start a fresh Codex session with:",
        "",
        f"    tokenpak codex \"Continue from failed request {record.request_id[:12]}...\"",
        "",
        "  Credential headers remain redacted in the record above.",
        "────────────────────────────────────────────────────────────────",
    ]

    print("\n".join(lines))
    return 0


__all__ = ["cmd_retry_drain", "cmd_codex_continue_last_failed"]
