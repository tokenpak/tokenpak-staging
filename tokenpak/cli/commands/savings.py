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
# Savings analytics — DELEGATED to the ONE unified helper.
#
# This module previously carried its own ``_query_savings`` /
# ``_query_by_model`` SQL with a hard-coded ``_MONITOR_DB = ""`` default and a
# bespoke ``input_tokens - compressed_tokens`` formula that was NOT
# attribution-aware (it conflated client cache with TokenPak compression). That
# was one of the ~4 independent savings computations D1 unifies. The dead path
# is removed; these thin wrappers now route through
# ``telemetry.unified_savings.savings_report`` so this surface can never invent
# its own math or resurface the old formula. The two attribution planes
# (TokenPak-earned compression vs client-attributed cache) are kept separate
# and never summed.
# ---------------------------------------------------------------------------


def _period_to_days(period: str) -> int:
    """Convert period string like '24h', '7d', '30d' to number of days."""
    period = period.strip()
    if period.endswith("h"):
        hours = int(period[:-1])
        return max(1, (hours + 23) // 24)
    if period.endswith("d"):
        return int(period[:-1])
    return 1


def _query_savings(period: str = "24h", model: str | None = None, db_path: str = "") -> dict:
    """Return the unified two-plane savings summary from the canonical feed.

    Routes through ``savings_report()``; the ``model`` filter is accepted for
    backward compatibility but the helper aggregates across the window. Returns
    a dict whose dollar figures are split into ``compression_savings_usd``
    (TokenPak-earned) and ``cache_savings_usd`` (credited to the client) — never
    a single combined number.
    """
    from tokenpak.telemetry.unified_savings import savings_report

    days = _period_to_days(period)
    report = savings_report(db_path=db_path or None, days=days)
    return {
        "requests": report.request_count,
        "db_state": report.db_state,
        "window": report.window,
        "compression_savings_usd": report.compression_savings.usd,
        "compressed_tokens": report.compression_savings.tokens,
        "cache_savings_usd": report.cache_savings.usd,
        "cache_tokens": report.cache_savings.tokens,
        "unattributable_usd": report.unattributable.usd,
        "total_cost": report.total_cost,
    }
