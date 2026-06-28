"""savings command — DEPRECATED. Use `tokenpak status` instead.

`tokenpak savings` is a legacy command. All savings data now appears in the
default `tokenpak status` output (savings-first layout, v3).

This wrapper prints a deprecation notice and then delegates to `tokenpak status`
with equivalent flags, so existing scripts and habits keep working.

Flag mapping:
    tokenpak savings              → tokenpak status
    tokenpak savings --verbose    → tokenpak status --full
    tokenpak savings --json       → tokenpak status --json
    tokenpak savings --period Xd  → (period ignored; status uses live DB)
"""

from __future__ import annotations

import sys

_DEPRECATION_NOTICE = """\
⚠️  `tokenpak savings` is deprecated.
    All savings data is now shown in `tokenpak status` (default view).
    Please update your workflow: tokenpak status
"""


def run_savings_cmd(args) -> None:
    """Dispatch handler for 'tokenpak savings' — prints notice then delegates to status."""
    print(_DEPRECATION_NOTICE)

    verbose = getattr(args, "verbose", False)
    as_json = getattr(args, "json", False) or getattr(args, "as_json", False)

    try:
        from tokenpak.cli.commands.status import _run_json, run, run_full

        if as_json:
            _run_json()
        elif verbose:
            run_full()
        else:
            run()
    except ImportError as exc:  # pragma: no cover
        print(f"  (Could not load status command: {exc})", file=sys.stderr)


# ---------------------------------------------------------------------------
# Click entrypoint (if available)
# ---------------------------------------------------------------------------

try:
    import click

    @click.command("savings")
    @click.option("--verbose", "-v", is_flag=True, help="[deprecated] Use tokenpak status --full")
    @click.option("--json", "as_json", is_flag=True, help="[deprecated] Use tokenpak status --json")
    @click.option("--period", default="24h", hidden=True, help="[deprecated] Ignored")
    def savings_cmd(verbose: bool, as_json: bool, period: str) -> None:
        """[DEPRECATED] Use `tokenpak status` instead.

        All savings data is now shown in the default `tokenpak status` output.
        """

        class _Args:
            pass

        a = _Args()
        a.verbose = verbose
        a.json = as_json
        a.as_json = as_json
        a.period = period
        run_savings_cmd(a)

except ImportError:
    savings_cmd = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# _query_savings / _query_by_model — savings analytics (used by tests)
# ---------------------------------------------------------------------------

_MONITOR_DB = ""


def _query_savings(period: str = "24h", model: str | None = None) -> dict:
    """Return aggregate savings summary from the monitor database."""
    path = _MONITOR_DB
    if not path:
        return {"error": "DB not found", "requests": 0}
    try:
        from tokenpak.cli.commands.status import _calculate_fleet_savings

        report = _calculate_fleet_savings(db_path=path, period=period)
        if report.get("error"):
            return {"error": report["error"], "requests": 0}
        models = report["models"]
        if model:
            models = [row for row in models if row["model"] == model]
        requests = sum(row["requests"] for row in models)
        total_compressed = sum(row["input_tokens"] for row in models)
        tokens_saved = sum(row["compressed_tokens"] for row in models)
        total_raw = total_compressed + tokens_saved
        reduction_pct = (tokens_saved / total_raw * 100.0) if total_raw > 0 else 0.0
        totals = report["totals"]
        if model:
            without_cost = sum(row["without_cost"] for row in models)
            with_cost = sum(row["with_cost"] for row in models)
            totals = {
                "without_cost": without_cost,
                "with_cost": with_cost,
                "savings_pct": (
                    (without_cost - with_cost) / without_cost * 100.0
                    if without_cost > 0
                    else 0.0
                ),
            }
        return {
            "requests": requests,
            "avg_raw_tokens": int(total_raw / requests) if requests else 0,
            "avg_compressed_tokens": int(total_compressed / requests) if requests else 0,
            "tokens_saved_total": int(tokens_saved),
            "reduction_pct": reduction_pct,
            "cost_without_tokenpak": totals["without_cost"],
            "cost_with_tokenpak": totals["with_cost"],
            "cost_reduction_pct": totals["savings_pct"],
        }
    except Exception:
        return {"error": "query failed", "requests": 0}


def _query_by_model(period: str = "24h", db_path: str = "") -> list:
    """Return per-model savings rows from the monitor database."""
    path = db_path or _MONITOR_DB
    if not path:
        return []
    try:
        from tokenpak.cli.commands.status import _calculate_fleet_savings

        report = _calculate_fleet_savings(db_path=path, period=period)
        if report.get("error"):
            return []
        rows = []
        for row in report["models"]:
            requests = row["requests"]
            total_compressed = row["input_tokens"]
            tokens_saved = row["compressed_tokens"]
            total_raw = total_compressed + tokens_saved
            rows.append({
                "model": row["model"],
                "requests": requests,
                "avg_raw_tokens": int(total_raw / requests) if requests else 0,
                "avg_compressed_tokens": int(total_compressed / requests) if requests else 0,
                "tokens_saved_total": int(tokens_saved),
                "reduction_pct": (tokens_saved / total_raw * 100.0) if total_raw > 0 else 0.0,
                "cost_without_tokenpak": row["without_cost"],
                "cost_with_tokenpak": row["with_cost"],
                "cost_reduction_pct": row["savings_pct"],
            })
        return rows
    except Exception:
        return []
