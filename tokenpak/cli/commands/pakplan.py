# SPDX-License-Identifier: Apache-2.0
"""``tokenpak pakplan`` CLI subcommand (Beta 1, Packet H).

PAKPlan is the planning surface that consumes the recall foundation
shipped at PR #184 / ``43bfb58e2c`` (recall reason/risk join tables +
Context Package ordering hints + advisory vocab lint registry).

Beta 1 OSS scope:
    preview        Static dry-run preview of what a PAKPlan would
                   surface for the current recall db. No scoring; no
                   capture-pipeline ingest. Honest about being preview.
    explain        Walk a single Pak's recall metadata + reason/risk
                   joins; show what the scorer *would* consider.
    report         One-shot rollup of the recall db + advisory-vocab
                   linter status (counts of forbidden-vocab matches in
                   Pak metadata, if any).

Scoring + the actual ranking pipeline + autonomous PAKPlan injection
remain Pro Local. This OSS surface is
deliberately read-only and never speaks to the Pro daemon.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Optional


def build_pakplan_parser(sub: Any) -> None:
    """Register the ``tokenpak pakplan`` subcommand."""
    p = sub.add_parser(
        "pakplan",
        help="Inspect the recall foundation; preview/explain/report (OSS)",
        description=(
            "Read-only consumer surface over the PAKPlan recall "
            "foundation. Scoring + capture pipeline are Pro."
        ),
    )
    psub = p.add_subparsers(dest="pakplan_action", required=False)

    p_preview = psub.add_parser(
        "preview", help="Dry-run preview of what a PAKPlan would surface"
    )
    p_preview.add_argument(
        "--limit", type=int, default=10,
        help="Max Paks to surface (default: 10)",
    )
    p_preview.add_argument(
        "--json", dest="as_json", action="store_true", help="Emit JSON",
    )
    p_preview.set_defaults(func=cmd_pakplan_preview)

    p_explain = psub.add_parser(
        "explain", help="Explain a single Pak's recall metadata"
    )
    p_explain.add_argument("pak_id", help="Pak id (e.g. pak:abcd1234…)")
    p_explain.add_argument(
        "--json", dest="as_json", action="store_true", help="Emit JSON",
    )
    p_explain.set_defaults(func=cmd_pakplan_explain)

    p_report = psub.add_parser(
        "report", help="Rollup of recall db + advisory vocab status"
    )
    p_report.add_argument(
        "--json", dest="as_json", action="store_true", help="Emit JSON",
    )
    p_report.set_defaults(func=cmd_pakplan_report)

    p_recall = psub.add_parser(
        "recall",
        help="Preview OSS recall candidates over the vault file index (BM25)",
    )
    _add_recall_query_args(p_recall)
    p_recall.add_argument(
        "--json", dest="as_json", action="store_true", help="Emit JSON",
    )
    p_recall.set_defaults(func=cmd_pakplan_recall)

    p_apply = psub.add_parser(
        "apply",
        help="Select/exclude recall candidates into an include/drop proof (OSS)",
    )
    _add_recall_query_args(p_apply)
    p_apply.add_argument(
        "--include", action="append", default=None, metavar="PAK_ID",
        help="Include only these pak_ids (repeatable); others are dropped",
    )
    p_apply.add_argument(
        "--exclude", action="append", default=None, metavar="PAK_ID",
        help="Exclude these pak_ids (repeatable); the rest are included",
    )
    p_apply.add_argument(
        "--reason", default=None, help="Free-text note recorded in the proof",
    )
    p_apply.add_argument(
        "--out", default=None,
        help="Write the proof JSON here (default: companion selections/ home)",
    )
    p_apply.add_argument(
        "--dry-run", dest="dry_run", action="store_true",
        help="Build and show the proof without writing it",
    )
    p_apply.add_argument(
        "--json", dest="as_json", action="store_true", help="Emit JSON",
    )
    p_apply.set_defaults(func=cmd_pakplan_apply)

    p.set_defaults(func=lambda a: p.print_help())


def _add_recall_query_args(parser: Any) -> None:
    """Shared recall-query arguments for the ``recall`` and ``apply`` surfaces."""
    parser.add_argument(
        "--query", "-q", default=None,
        help="BM25 query over the vault file index",
    )
    parser.add_argument(
        "--project", default=None,
        help="Client-side filter: keep candidates from this project",
    )
    parser.add_argument(
        "--type", dest="pak_type", default=None,
        help="Client-side filter: keep candidates of this pak_type",
    )
    parser.add_argument(
        "--limit", type=int, default=20, help="Max candidates (default: 20)",
    )


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def cmd_pakplan_preview(args: Any) -> int:
    db = _recall_db()
    rows = _query_paks(db, limit=int(getattr(args, "limit", 10)))
    paks = [_pak_summary(r) for r in rows]

    payload = {
        "scope": "preview",
        "scoring": "not-shipped-in-OSS",
        "note": (
            "Beta 1 OSS preview is unscored. Pro Local adds the scorer + "
            "ranking pipeline."
        ),
        "recall_db": str(db) if db else None,
        "pak_count": len(paks),
        "paks": paks,
    }
    if getattr(args, "as_json", False):
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    print("PAKPlan preview (OSS, unscored)")
    print("───────────────────────────────")
    if not db or not db.exists():
        print(f"ℹ️  No recall db found at {db}")
        print("   The foundation tables ship in Beta 1 OSS; the capture "
              "pipeline that populates them is Pro.")
        return 0
    if not paks:
        print(f"ℹ️  Recall db exists at {db} but contains no Paks yet.")
        return 0
    for p in paks:
        print(f"  • {p['pak_id']}  {p.get('title', '')[:40]}")
        if p.get("reason_codes"):
            print(f"      reasons: {', '.join(p['reason_codes'])}")
        if p.get("risk_flags"):
            print(f"      risks  : {', '.join(p['risk_flags'])}")
    print()
    print("Scoring is Pro — install tokenpak-paid for ranked previews.")
    return 0


def cmd_pakplan_explain(args: Any) -> int:
    db = _recall_db()
    if not db or not db.exists():
        msg = "no recall db on disk"
        if getattr(args, "as_json", False):
            print(json.dumps({"error": "no_recall_db", "detail": msg}))
        else:
            print(f"✗ tokenpak pakplan explain — {msg}", file=sys.stderr)
        return 1
    row = _query_pak_by_id(db, args.pak_id)
    if not row:
        msg = f"pak not in recall db: {args.pak_id}"
        if getattr(args, "as_json", False):
            print(json.dumps({"error": "pak_not_found", "detail": msg}))
        else:
            print(f"✗ tokenpak pakplan explain — {msg}", file=sys.stderr)
        return 1
    summary = _pak_summary(row)
    summary["scoring"] = "not-shipped-in-OSS"
    if getattr(args, "as_json", False):
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0
    print(f"Pak {summary['pak_id']}")
    print("─" * 50)
    for k, v in summary.items():
        if k == "pak_id":
            continue
        print(f"  {k:20s}: {v}")
    return 0


def cmd_pakplan_report(args: Any) -> int:
    db = _recall_db()
    if not db or not db.exists():
        payload = {
            "recall_db": str(db) if db else None,
            "present": False,
            "pak_count": 0,
            "reason_code_counts": {},
            "risk_flag_counts": {},
            "advisory_vocab": {"checked": False,
                              "note": "no Paks to lint"},
        }
        if getattr(args, "as_json", False):
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print("PAKPlan report")
            print("──────────────")
            print(f"ℹ️  No recall db at {db}")
            print("   Foundation tables ship in OSS; population is Pro.")
        return 0

    rows = _query_paks(db, limit=10_000)
    summaries = [_pak_summary(r) for r in rows]
    reason_counts: dict[str, int] = {}
    risk_counts: dict[str, int] = {}
    for s in summaries:
        for r in s.get("reason_codes", []) or []:
            reason_counts[r] = reason_counts.get(r, 0) + 1
        for r in s.get("risk_flags", []) or []:
            risk_counts[r] = risk_counts.get(r, 0) + 1

    payload = {
        "recall_db": str(db),
        "present": True,
        "pak_count": len(summaries),
        "reason_code_counts": reason_counts,
        "risk_flag_counts": risk_counts,
        "advisory_vocab": {
            "checked": True,
            "matches": _advisory_vocab_matches(summaries),
        },
    }
    if getattr(args, "as_json", False):
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    print("PAKPlan report")
    print("──────────────")
    print(f"  recall db   : {db}")
    print(f"  pak count   : {len(summaries)}")
    if reason_counts:
        print("  reason codes:")
        for k, v in sorted(reason_counts.items(), key=lambda kv: -kv[1]):
            print(f"    {k:30s} {v}")
    if risk_counts:
        print("  risk flags  :")
        for k, v in sorted(risk_counts.items(), key=lambda kv: -kv[1]):
            print(f"    {k:30s} {v}")
    matches = payload["advisory_vocab"]["matches"]
    print(f"  advisory vocab matches: {len(matches)}")
    return 0


def cmd_pakplan_recall(args: Any) -> int:
    """Preview OSS recall candidates over the vault FILE index.

    Ranked retrieval is BM25 over the vault file index — never the recall /
    Pak store (``paks`` / ``paks_fts`` / ``pak_relations``), which is
    Pro-reserved by policy. ``score`` is the real BM25 relevance.
    """
    candidates = _vault_recall_candidates(args)
    payload = {
        "scope": "recall-preview",
        "boundary": "oss_vault_file_index",
        "scoring": "bm25-vault-file-index",
        "query": getattr(args, "query", None),
        "count": len(candidates),
        "candidates": candidates,
    }
    if getattr(args, "as_json", False):
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    print("PAKPlan recall preview (OSS — vault file index, BM25)")
    print("─────────────────────────────────────────────────────")
    if not candidates:
        if not (getattr(args, "query", None) or "").strip():
            print("ℹ️  Provide --query to search the vault file index.")
        else:
            print("ℹ️  No matching vault blocks (no vault index, or no matches).")
        return 0
    for c in candidates:
        src = c.get("source", {})
        print(f"  {c['rank']:>2}. {c['pak_id']}  {(c.get('title') or '')[:40]}")
        print(f"      source : {src.get('source_type')}/{src.get('authority')}"
              f"  score: {c.get('score')}")
        if c.get("snippet"):
            print(f"      snippet: {c['snippet'][:80]}")
        if c.get("risk"):
            print(f"      risk   : {c['risk']}")
    print()
    print("Ranked retrieval over the recall/Pak store is Pro;")
    print("OSS ranks the vault file index only.")
    return 0


def cmd_pakplan_apply(args: Any) -> int:
    """Apply/exclude recall candidates into an include/drop proof object.

    Candidates come from the OSS vault FILE index; the proof is
    inspectable and reversible (a JSON file you can read or delete). Receipt v1
    consumes its ``included`` / ``dropped`` lists. OSS never auto-applies the
    selection to a live request, and this path never reads the recall / Pak
    store — the include/drop shaper is a pure, store-agnostic transform.
    """
    from tokenpak.companion.recall.store import RecallStore

    candidates = _vault_recall_candidates(args)
    selection = RecallStore.build_context_selection(
        candidates,
        include=getattr(args, "include", None),
        exclude=getattr(args, "exclude", None),
        query=getattr(args, "query", None),
        reason=getattr(args, "reason", None),
    )
    out_path: Optional[Path] = None
    if not getattr(args, "dry_run", False):
        raw_out = getattr(args, "out", None)
        out_path = RecallStore.write_context_selection(
            selection, path=Path(raw_out) if raw_out else None
        )

    payload = dict(selection)
    payload["proof_path"] = str(out_path) if out_path else None
    payload["dry_run"] = bool(getattr(args, "dry_run", False))
    if getattr(args, "as_json", False):
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    counts = selection.get("counts", {})
    print("PAKPlan apply — context selection proof (OSS)")
    print("─────────────────────────────────────────────")
    print(f"  selection : {selection.get('selection_id')}")
    print(f"  included  : {counts.get('included', 0)}")
    print(f"  dropped   : {counts.get('dropped', 0)}")
    for c in selection.get("included", []):
        print(f"    ✓ {c['pak_id']}  {(c.get('title') or '')[:40]}")
    for c in selection.get("dropped", []):
        print(f"    ✗ {c['pak_id']}  ({c.get('drop_reason')})")
    if selection.get("unknown_ids"):
        print(f"  ⚠️  unknown ids: {', '.join(selection['unknown_ids'])}")
    if out_path:
        print(f"  proof     : {out_path}")
    else:
        print("  proof     : (dry-run — not written)")
    return 0


# ---------------------------------------------------------------------------
# Recall candidate source (OSS vault FILE index)
# ---------------------------------------------------------------------------


def _vault_recall_candidates(args: Any) -> list[dict]:
    """OSS recall candidates ranked over the vault FILE index.

    Retrieval is BM25 over the vault file index via
    :func:`tokenpak.vault.pak_adapter.recall_preview_candidates` — never the
    recall / Pak store, which is Pro-reserved. ``--query`` drives the BM25
    search; ``--project`` / ``--type`` are applied as honest client-side
    filters over the returned candidates (``rank`` is renumbered contiguously
    after filtering). An empty query or unavailable vault index yields ``[]``.
    """
    from tokenpak.vault import pak_adapter

    candidates = pak_adapter.recall_preview_candidates(
        query=getattr(args, "query", None),
        top_k=int(getattr(args, "limit", 20) or 20),
    )

    project = getattr(args, "project", None)
    pak_type = getattr(args, "pak_type", None)
    if project:
        candidates = [
            c for c in candidates if c.get("source", {}).get("project") == project
        ]
    if pak_type:
        candidates = [
            c for c in candidates if c.get("source", {}).get("pak_type") == pak_type
        ]
    for i, c in enumerate(candidates):
        c["rank"] = i + 1
    return candidates


# ---------------------------------------------------------------------------
# Recall db helpers (inspect/list surfaces: preview / explain / report)
# ---------------------------------------------------------------------------


def _recall_db() -> Optional[Path]:
    """Resolve the recall db path under the canonical TokenPak home."""
    from tokenpak import _paths

    return _paths.under("companion", "recall.db")


def _query_paks(db: Path, *, limit: int) -> list[dict]:
    """Return up to ``limit`` Pak rows joined with their reasons + risks.

    Reason/risk metadata lives in the recall store's ``pak_reason_codes``
    and ``pak_risk_flags`` tables. We probe for the Pak table presence to
    stay forward-compatible with future renames.
    """
    if not db.exists():
        return []
    try:
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
    except sqlite3.Error:
        return []
    try:
        tables = {
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        pak_table = (
            "paks" if "paks" in tables
            else "pak" if "pak" in tables
            else None
        )
        if pak_table is None:
            return []
        rows = list(conn.execute(
            f"SELECT * FROM {pak_table} ORDER BY rowid DESC LIMIT ?", (limit,)
        ))
        out: list[dict] = []
        for r in rows:
            d = dict(r)
            d["_reason_codes"] = _join_codes(conn, "pak_reason_codes", d.get("pak_id"))
            d["_risk_flags"] = _join_codes(conn, "pak_risk_flags", d.get("pak_id"))
            out.append(d)
        return out
    finally:
        conn.close()


def _query_pak_by_id(db: Path, pak_id: str) -> Optional[dict]:
    rows = _query_paks(db, limit=10_000)
    for r in rows:
        if r.get("pak_id") == pak_id:
            return r
    return None


def _join_codes(conn: sqlite3.Connection, table: str, pak_id: Optional[str]) -> list[str]:
    if not pak_id:
        return []
    try:
        rows = list(conn.execute(
            f"SELECT * FROM {table} WHERE pak_id = ?", (pak_id,)
        ))
    except sqlite3.Error:
        return []
    out: list[str] = []
    for r in rows:
        d = dict(r)
        for k in ("code", "reason_code", "flag", "risk_flag"):
            if k in d and d[k]:
                out.append(str(d[k]))
                break
    return out


def _pak_summary(row: dict) -> dict:
    return {
        "pak_id": row.get("pak_id") or row.get("id") or "?",
        "title": row.get("title") or row.get("name") or "",
        "created_at": row.get("created_at") or row.get("ts") or "",
        "reason_codes": row.get("_reason_codes", []),
        "risk_flags": row.get("_risk_flags", []),
    }


def _advisory_vocab_matches(summaries: list[dict]) -> list[dict]:
    """Apply the advisory-vocab lint to Pak titles/summaries.

    Beta 1 OSS uses a small embedded list. Future versions read from
    ``tokenpak.companion.recall.vocab.forbidden_vocabulary`` so the
    registry is the single source of truth.
    """
    try:
        from tokenpak.companion.recall import vocab as _vocab  # type: ignore[attr-defined]
        forbidden = list(getattr(_vocab, "FORBIDDEN", []) or [])
    except Exception:
        forbidden = ["TODO-WIP", "DRAFT-DRAFT", "PLACEHOLDER"]
    if not forbidden:
        return []
    out: list[dict] = []
    for s in summaries:
        text = " ".join(str(s.get(k, "")) for k in ("title", "pak_id"))
        for term in forbidden:
            if term in text:
                out.append({"pak_id": s["pak_id"], "term": term})
    return out


__all__ = [
    "build_pakplan_parser",
    "cmd_pakplan_preview",
    "cmd_pakplan_explain",
    "cmd_pakplan_report",
]
