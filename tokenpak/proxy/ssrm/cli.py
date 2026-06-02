"""SSRM CLI surface — ``tokenpak ssrm {status,tail,explain}``.

Invocation:
    python -m tokenpak.proxy.ssrm.cli status
    python -m tokenpak.proxy.ssrm.cli tail [--session=<sid>] [--limit=N]
    python -m tokenpak.proxy.ssrm.cli explain <decision_id>

The main ``tokenpak`` CLI can dispatch to this module via a thin
sub-command wrapper in ``tokenpak/cli/commands/ssrm.py``; this module
is the source of truth.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Optional

from .audit import explain, summary_stats, tail
from .policy import _get_ssrm_config


def _fmt_ts(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def cmd_status(args) -> int:
    """`tokenpak ssrm status` — read-only summary."""
    cfg = _get_ssrm_config()
    audit_db = os.path.expanduser(cfg.get("audit_db_path", "~/.tokenpak/ssrm_audit.db"))
    enabled = bool(cfg.get("enabled"))
    stats = summary_stats(audit_db_path=audit_db)
    if args.json:
        out = {
            "enabled": enabled,
            "audit_db": audit_db,
            "stats": stats,
            "recent": tail(audit_db_path=audit_db, limit=5),
        }
        print(json.dumps(out, indent=2, default=str))
        return 0

    print("SSRM Phase 1 (instrumentation-only)")
    print(f"  enabled            = {enabled}")
    print(f"  audit_db           = {audit_db}")
    print(f"  total_decisions    = {stats.get('total_decisions', 0)}")
    print(f"  avg_eff_ctx_pct_24h = {stats.get('avg_effective_context_pct_24h')}")
    by_action = stats.get("by_action") or {}
    if by_action:
        print("  by_action:")
        for k, v in sorted(by_action.items(), key=lambda x: -x[1]):
            print(f"    {k:<35s} {v}")
    top_fp = stats.get("top_sessions_by_repeat") or []
    if top_fp:
        print("  top sessions by fingerprint repeat:")
        for r in top_fp:
            print(f"    {r.get('session_id','')!s:<40s}  {r.get('max_repeat')}")
    top_eff = stats.get("top_sessions_by_effective_context") or []
    if top_eff:
        print("  top sessions by effective context %:")
        for r in top_eff:
            print(f"    {r.get('session_id','')!s:<40s}  {r.get('max_eff_ctx_pct')}")
    rows = tail(audit_db_path=audit_db, limit=5)
    if rows:
        print("  recent decisions (5 most recent):")
        for r in rows:
            print(f"    [{_fmt_ts(r['ts'])}] {r.get('agent_id') or '?':<6s} "
                  f"{r.get('model','?'):<22s} {r.get('decision','?'):<32s} "
                  f"eff_ctx_pct={r.get('effective_context_pct')} "
                  f"reason={r.get('reason','')[:50]}")
    return 0


def cmd_tail(args) -> int:
    """`tokenpak ssrm tail` — stream-style tail of audit log."""
    cfg = _get_ssrm_config()
    audit_db = os.path.expanduser(cfg.get("audit_db_path", "~/.tokenpak/ssrm_audit.db"))
    rows = tail(audit_db_path=audit_db, session_id=args.session, limit=int(args.limit))
    if args.json:
        print(json.dumps(rows, indent=2, default=str))
        return 0
    if not rows:
        print("(no decisions yet)")
        return 0
    for r in rows:
        print(f"[{_fmt_ts(r['ts'])}] id={r['id']} sess={r.get('session_id') or '?':<20s} "
              f"action={r['decision']:<32s} eff_ctx={r.get('effective_context_pct')} "
              f"crr={r.get('cache_read_ratio')} fp_rep={r.get('fingerprint_repeat_count')} "
              f"progress={r.get('progress_signal')} reason={r.get('reason','')[:80]}")
    return 0


def cmd_explain(args) -> int:
    """`tokenpak ssrm explain <id>` — full signals_json for one decision."""
    cfg = _get_ssrm_config()
    audit_db = os.path.expanduser(cfg.get("audit_db_path", "~/.tokenpak/ssrm_audit.db"))
    row = explain(int(args.decision_id), audit_db_path=audit_db)
    if not row:
        print(f"no decision found with id={args.decision_id}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(row, indent=2, default=str))
        return 0
    print(f"decision id={row['id']}")
    print(f"  ts          : {_fmt_ts(row['ts'])}")
    print(f"  session_id  : {row.get('session_id')}")
    print(f"  agent_id    : {row.get('agent_id')}")
    print(f"  model       : {row.get('model')}")
    print(f"  decision    : {row['decision']}")
    print(f"  advisory_only: {bool(row.get('advisory_only'))}")
    print(f"  reason      : {row.get('reason')}")
    try:
        sigs = json.loads(row.get("signals_json") or "{}")
    except Exception:
        sigs = {}
    print("  signals:")
    for k in sorted(sigs.keys()):
        print(f"    {k:<32s} = {sigs[k]}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tokenpak ssrm",
        description="SSRM Phase 1 — instrumentation-only read-only surface",
    )
    sub = parser.add_subparsers(dest="ssrm_cmd", required=False)

    p_status = sub.add_parser("status", help="Show enabled state + audit summary")
    p_status.add_argument("--json", action="store_true", help="Machine-readable JSON output")

    p_tail = sub.add_parser("tail", help="Tail recent SSRM decisions")
    p_tail.add_argument("--session", default=None, help="Filter by session_id")
    p_tail.add_argument("--limit", default=20, help="Max rows (default 20)")
    p_tail.add_argument("--json", action="store_true")

    p_explain = sub.add_parser("explain", help="Show full signals for one decision id")
    p_explain.add_argument("decision_id", help="The audit row id (see tail output)")
    p_explain.add_argument("--json", action="store_true")

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.ssrm_cmd:
        parser.print_help()
        return 0
    handlers = {"status": cmd_status, "tail": cmd_tail, "explain": cmd_explain}
    return handlers[args.ssrm_cmd](args)


if __name__ == "__main__":
    sys.exit(main())
