# SPDX-License-Identifier: Apache-2.0
"""Savings attribution v2 — TIP-06.

Provides parsing helpers that extract per-source savings from provider API
response usage fields, and aggregation utilities for reporting.

Attribution rules (per TIP telemetry_contract.SavingsSource):
- Provider/platform cache MUST be labelled PROVIDER_PROMPT_CACHE or
  PLATFORM_CACHE; never credited to TokenPak.
- TokenPak-managed stages (semantic cache, compression, capsules, etc.)
  are labelled with the appropriate TOKENPAK_* source.
- Unknown deltas use UNATTRIBUTED.
- If model pricing is unavailable, tokens are reported but
  estimated_cost_saved is None and cost_available=False.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from tokenpak.tip.telemetry_contract import SavingsSource
from tokenpak.tip.trace_contract import SavingsAttribution

# ---------------------------------------------------------------------------
# OpenAI usage parsing
# ---------------------------------------------------------------------------


def parse_openai_usage(
    usage: Dict[str, Any],
    *,
    model: str = "",
    pricing: Optional[Dict[str, float]] = None,
) -> List[SavingsAttribution]:
    """Parse an OpenAI Responses/Chat completion usage dict into attributions.

    Extracts ``prompt_tokens_details.cached_tokens`` as PLATFORM_CACHE.
    If no provider/platform cache is detected, returns an empty list (not
    a fake unattributed record — unattributed is only emitted when a token
    delta is observed but cannot be sourced).

    Parameters
    ----------
    usage:
        The ``usage`` sub-dict from an OpenAI API response.
    model:
        Model name, used for cost lookup.
    pricing:
        Optional dict with ``input_per_token`` and ``output_per_token`` keys.
        When None, token counts are reported but cost estimates are omitted.
    """
    results: List[SavingsAttribution] = []
    if not usage:
        return results

    # prompt_tokens_details.cached_tokens → PLATFORM_CACHE
    details = usage.get("prompt_tokens_details") or {}
    cached_tokens = int(details.get("cached_tokens", 0) or 0)

    if cached_tokens > 0:
        cost_saved: Optional[float] = None
        cost_available = False
        if pricing and "input_per_token" in pricing:
            cost_saved = round(cached_tokens * pricing["input_per_token"], 8)
            cost_available = True

        results.append(
            SavingsAttribution(
                source=SavingsSource.PLATFORM_CACHE,
                raw_tokens=int(usage.get("prompt_tokens", 0) or 0) + cached_tokens,
                sent_tokens=int(usage.get("prompt_tokens", 0) or 0),
                saved_tokens=cached_tokens,
                estimated_cost_saved=cost_saved,
                cost_available=cost_available,
                notes=f"openai cached_tokens={cached_tokens}",
            )
        )

    return results


# ---------------------------------------------------------------------------
# Anthropic usage parsing
# ---------------------------------------------------------------------------


def parse_anthropic_usage(
    usage: Dict[str, Any],
    *,
    model: str = "",
    pricing: Optional[Dict[str, float]] = None,
) -> List[SavingsAttribution]:
    """Parse an Anthropic API response usage dict into attributions.

    Extracts ``cache_read_input_tokens`` as PROVIDER_PROMPT_CACHE.

    Parameters
    ----------
    usage:
        The ``usage`` sub-dict from an Anthropic API response.
    model:
        Model name, used for cost lookup.
    pricing:
        Optional dict with ``cache_read_per_token`` key.
        When None, token counts are reported but cost estimates are omitted.
    """
    results: List[SavingsAttribution] = []
    if not usage:
        return results

    cache_read = int(usage.get("cache_read_input_tokens", 0) or 0)

    if cache_read > 0:
        cost_saved: Optional[float] = None
        cost_available = False
        input_tokens = int(usage.get("input_tokens", 0) or 0)

        if pricing and "input_per_token" in pricing:
            # Anthropic cache_read is billed at 0.10× input rate; savings vs
            # full price = 0.90× input per cached token.
            savings_rate = pricing["input_per_token"] * 0.90
            cost_saved = round(cache_read * savings_rate, 8)
            cost_available = True

        results.append(
            SavingsAttribution(
                source=SavingsSource.PROVIDER_PROMPT_CACHE,
                raw_tokens=input_tokens + cache_read,
                sent_tokens=input_tokens,
                saved_tokens=cache_read,
                estimated_cost_saved=cost_saved,
                cost_available=cost_available,
                notes=f"anthropic cache_read_input_tokens={cache_read}",
            )
        )

    return results


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


@dataclass
class SourceSummary:
    """Aggregate savings metrics for one attribution source."""

    source: str
    saved_tokens: int = 0
    estimated_cost_saved: float = 0.0
    cost_available: bool = False
    request_count: int = 0
    credited_to_tokenpak: bool = False


def aggregate_attributions(
    attributions: Sequence[SavingsAttribution],
) -> Dict[str, SourceSummary]:
    """Group attributions by source and sum token/cost figures."""
    result: Dict[str, SourceSummary] = {}
    for attr in attributions:
        if attr.source not in result:
            result[attr.source] = SourceSummary(
                source=attr.source,
                credited_to_tokenpak=attr.source in SavingsSource.TOKENPAK_MANAGED,
            )
        summary = result[attr.source]
        summary.saved_tokens += attr.saved_tokens
        summary.request_count += 1
        if attr.estimated_cost_saved is not None:
            summary.estimated_cost_saved += attr.estimated_cost_saved
            summary.cost_available = True
    return result


def format_savings_by_source(
    by_source: Dict[str, SourceSummary],
    *,
    days: int = 7,
) -> str:
    """Return a human-readable savings breakdown by attribution source."""
    if not by_source:
        return "No savings attribution data for this period."

    total_tp_tokens = sum(
        s.saved_tokens for s in by_source.values() if s.credited_to_tokenpak
    )
    total_ext_tokens = sum(
        s.saved_tokens for s in by_source.values() if not s.credited_to_tokenpak
        and s.source != SavingsSource.UNATTRIBUTED
    )
    total_cost = sum(
        s.estimated_cost_saved for s in by_source.values() if s.cost_available
    )

    lines = [
        f"Savings Attribution — Last {days} Days",
        "─" * 48,
        "",
    ]

    # TokenPak-managed savings first
    tp_entries = [s for s in by_source.values() if s.credited_to_tokenpak]
    if tp_entries:
        lines.append("TokenPak-managed:")
        for s in sorted(tp_entries, key=lambda x: -x.saved_tokens):
            cost_str = f"  ${s.estimated_cost_saved:.4f}" if s.cost_available else "  (price N/A)"
            lines.append(f"  {s.source:<38} {s.saved_tokens:>8} tok{cost_str}")

    # Provider/platform savings (not credited to TokenPak)
    ext_entries = [
        s for s in by_source.values()
        if not s.credited_to_tokenpak and s.source != SavingsSource.UNATTRIBUTED
    ]
    if ext_entries:
        lines.append("")
        lines.append("Provider/Platform (not credited to TokenPak):")
        for s in sorted(ext_entries, key=lambda x: -x.saved_tokens):
            cost_str = f"  ${s.estimated_cost_saved:.4f}" if s.cost_available else "  (price N/A)"
            lines.append(f"  {s.source:<38} {s.saved_tokens:>8} tok{cost_str}")

    # Unattributed
    unattr = by_source.get(SavingsSource.UNATTRIBUTED)
    if unattr and unattr.saved_tokens > 0:
        lines.append("")
        lines.append(f"  unattributed                           {unattr.saved_tokens:>8} tok")

    lines.append("")
    if total_tp_tokens > 0 or total_ext_tokens > 0:
        lines.append(f"  TokenPak-managed total:  {total_tp_tokens:>8} tokens saved")
        lines.append(f"  Provider/platform total: {total_ext_tokens:>8} tokens (not overclaimed)")
    if total_cost > 0:
        lines.append(f"  Estimated cost saved:    ${total_cost:.4f}")
    else:
        lines.append("  Estimated cost saved:    (configure model pricing for cost estimates)")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# DB row helpers (used by TelemetryDB)
# ---------------------------------------------------------------------------


def attribution_to_row(
    request_id: str,
    attribution: SavingsAttribution,
    *,
    timestamp: Optional[float] = None,
    platform: Optional[str] = None,
    model: str = "",
) -> Dict[str, Any]:
    """Serialize a SavingsAttribution into a tp_savings_attribution row dict."""
    return {
        "request_id": request_id,
        "timestamp": timestamp or time.time(),
        "source": attribution.source,
        "raw_tokens": attribution.raw_tokens,
        "sent_tokens": attribution.sent_tokens,
        "saved_tokens": attribution.saved_tokens,
        "estimated_cost_saved": attribution.estimated_cost_saved or 0.0,
        "cost_available": int(attribution.cost_available),
        "credited_to_tokenpak": int(attribution.credited_to_tokenpak),
        "platform": platform or "",
        "model": model,
        "notes": attribution.notes or "",
    }


# ---------------------------------------------------------------------------
# Canonical savings metric (TPK-SAVINGS-001) — single source of truth
#
# One computation shared by ``doctor``, ``savings``, ``status``, and ``cost``
# so a single install can never report three different savings numbers.
# Attribution is conservative (the ``status`` gold standard): a request row
# contributes savings ONLY when TokenPak actually caused the reduction —
#   * compression is credited only when cache_origin == 'proxy' (proxy-caused);
#   * cache reads are credited only for proxy-managed cache (cache_origin='proxy').
# Rows with compressed_tokens == 0 OR cache_origin != 'proxy' therefore
# contribute **0 saved** — never the historical 100%-overclaim that
# SUM(input_tokens - compressed_tokens) produced when compressed_tokens was 0.
#
# The per-model math here is the same proven computation that
# ``status._calculate_fleet_savings`` has used; that function now delegates to
# this engine so status/doctor and the live ``savings``/``cost`` surfaces all
# agree. The window resolution mirrors ``status._window_clause`` for the period
# tokens status passes (None / today / 1h / 24h / 7d / 30d / <N>h_custom /
# <N>{m,h,d,mo}) so delegation is behaviour-preserving, and adds the extra
# tokens the other surfaces need (all / last100 / session / <N>d_custom /
# week / month / yesterday).
# ---------------------------------------------------------------------------


@dataclass
class SavingsResult:
    """Canonical savings figures for one window, shared by every CLI surface."""

    window: str
    window_label: str
    requests: int = 0
    saved_tokens: int = 0          # proxy-attributed compressed tokens removed
    saved_cost: float = 0.0        # conservative $ saved (compression + proxy cache)
    baseline_cost: float = 0.0     # estimated cost WITHOUT TokenPak
    actual_cost: float = 0.0       # estimated cost WITH TokenPak
    savings_pct: float = 0.0       # saved_cost / baseline_cost * 100
    cache_savings: float = 0.0
    compression_savings: float = 0.0
    routing_savings: float = 0.0
    claude_code_cache_savings: float = 0.0
    models: List[Dict[str, Any]] = field(default_factory=list)
    db_rows: int = 0
    error: Optional[str] = None


# Period→SQL maps mirror status._window_clause / _PERIOD_MAP / _WINDOW_RE /
# _UNIT_SQL so that ``status`` delegation produces byte-identical SQL.
_SAVINGS_PERIOD_MAP = {"1h": "-1 hours", "24h": "-1 days", "7d": "-7 days", "30d": "-30 days"}
_SAVINGS_WINDOW_RE = re.compile(r"^(\d+)(m|h|d|mo)$")
_SAVINGS_UNIT_SQL = {"m": "minutes", "h": "hours", "d": "days", "mo": "months"}


def _savings_window_spec(window: Any) -> tuple:
    """Map a window token to ``(where_sql, params, row_limit, label)``.

    For the tokens ``status`` passes (``None`` / ``today`` / ``1h`` / ``24h`` /
    ``7d`` / ``30d`` / ``<N>h_custom`` / ``<N>{m,h,d,mo}``) the returned
    ``(where_sql, params)`` is identical to ``status._window_clause`` so the
    delegating ``_calculate_fleet_savings`` cannot change status's results.
    Additional tokens (``all`` / ``last100`` / ``session`` / ``<N>d_custom`` /
    ``week`` / ``month`` / ``yesterday``) serve the ``savings`` / ``cost`` /
    ``doctor`` surfaces.
    """
    if window is None:
        return "", [], None, "all-time"
    w = str(window).strip().lower()
    if w in ("", "all", "all-time", "none"):
        return "", [], None, "all-time"
    if w in ("last100", "last-100"):
        return "", [], 100, "last 100 reqs"
    if w == "session":
        return "", [], 100, "this session"
    # status._window_clause parity branches (order matters) ------------------
    if w == "today":
        return "WHERE datetime(timestamp, 'localtime') >= date('now', 'localtime')", [], None, "today"
    if w in _SAVINGS_PERIOD_MAP:
        return "WHERE timestamp >= datetime('now', ?)", [_SAVINGS_PERIOD_MAP[w]], None, f"last {w}"
    if w.endswith("h_custom"):
        try:
            hours = int(w[: -len("h_custom")])
        except ValueError:
            return "", [], None, "all-time"
        return "WHERE timestamp >= datetime('now', ?)", [f"-{hours} hours"], None, f"last {hours}h"
    # extra (non-status) tokens ---------------------------------------------
    if w == "yesterday":
        return (
            "WHERE date(timestamp, 'localtime') = date('now', '-1 day', 'localtime')",
            [],
            None,
            "yesterday",
        )
    if w == "week":
        return "WHERE timestamp >= datetime('now', ?)", ["-7 days"], None, "last 7 days"
    if w == "month":
        return "WHERE timestamp >= datetime('now', ?)", ["-30 days"], None, "this month"
    if w.endswith("d_custom"):
        try:
            days = int(w[: -len("d_custom")])
        except ValueError:
            return "", [], None, "all-time"
        return "WHERE timestamp >= datetime('now', ?)", [f"-{days} days"], None, f"last {days}d"
    m = _SAVINGS_WINDOW_RE.match(w)
    if m:
        return (
            "WHERE timestamp >= datetime('now', ?)",
            [f"-{m.group(1)} {_SAVINGS_UNIT_SQL[m.group(2)]}"],
            None,
            f"last {w}",
        )
    return "", [], None, "all-time"


def compute_savings(window: Any = "all", db_path: Optional[str] = None) -> SavingsResult:
    """Return the canonical token/cost savings for ``window``.

    This is the single source of truth all savings-reporting CLI surfaces
    (``doctor``, ``savings``, ``status``, ``cost``) derive from, so they
    cannot disagree on the same install. Attribution is conservative: only
    proxy-caused compression and proxy-managed cache reads count as savings;
    rows with ``compressed_tokens == 0`` or ``cache_origin != 'proxy'``
    contribute **0 saved**.

    Parameters
    ----------
    window:
        ``all`` / ``session`` / ``last100`` / ``today`` / ``yesterday`` /
        ``1h`` / ``24h`` / ``7d`` / ``30d`` / ``<N>h_custom`` /
        ``<N>d_custom`` / ``week`` / ``month`` / ``<N>{m,h,d,mo}`` (or ``None``
        == all-time).
    db_path:
        Optional monitor.db path. When ``None``, resolves the canonical DB via
        ``tokenpak._paths.monitor_db(mode='read')`` — the SAME candidate chain
        ``status``, ``doctor``, ``cost``, and the proxy writer all use.
    """
    import sqlite3

    where_sql, params, row_limit, label = _savings_window_spec(window)
    window_key = "all" if window is None else str(window)

    if db_path is None:
        try:
            from tokenpak import _paths

            resolved = _paths.monitor_db(mode="read")
            db_path = str(resolved) if resolved else None
        except Exception:
            db_path = None
    if not db_path or not Path(db_path).exists():
        return SavingsResult(window=window_key, window_label=label, error="db_not_found")

    try:
        from tokenpak.models import get_rates
    except Exception:  # pragma: no cover - pricing registry unavailable
        def get_rates(model: Optional[str] = None) -> Dict[str, float]:
            return {"input": 3.0, "cached": 0.30, "output": 15.0}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        col_names = {r[1] for r in conn.execute("PRAGMA table_info(requests)").fetchall()}
        if "model" not in col_names:
            return SavingsResult(window=window_key, window_label=label, error="no_requests_table")
        has_origin = "cache_origin" in col_names

        # Proxy-credited compression / cache: only when proxy owned the cache.
        compressed_expr = (
            "COALESCE(SUM(CASE WHEN cache_origin = 'proxy' "
            "THEN compressed_tokens ELSE 0 END), 0)"
            if has_origin
            else "0"
        )
        proxy_cr_expr = (
            "COALESCE(SUM(CASE WHEN COALESCE(cache_origin, 'unknown') = 'proxy' "
            "THEN cache_read_tokens ELSE 0 END), 0)"
            if has_origin
            else "0"
        )
        inner = "requests"
        if row_limit is not None:
            inner = f"(SELECT * FROM requests ORDER BY id DESC LIMIT {int(row_limit)})"
        sql = f"""
            SELECT
                model,
                COUNT(*) AS requests,
                COALESCE(SUM(input_tokens), 0) AS input_tokens,
                COALESCE(SUM(output_tokens), 0) AS output_tokens,
                COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                {compressed_expr} AS compressed_tokens,
                COALESCE(SUM(estimated_cost), 0.0) AS estimated_cost,
                {proxy_cr_expr} AS proxy_managed_cache_read
            FROM {inner}
            {where_sql}
            GROUP BY model
            ORDER BY SUM(input_tokens) DESC
        """
        rows = conn.execute(sql, params).fetchall()
        try:
            db_rows = conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
        except Exception:
            db_rows = 0
    except sqlite3.Error as exc:
        return SavingsResult(window=window_key, window_label=label, error=str(exc))
    finally:
        try:
            conn.close()
        except Exception:
            pass

    result = SavingsResult(window=window_key, window_label=label, db_rows=db_rows)
    if not rows:
        return result

    total_without = 0.0
    total_with = 0.0
    total_cache = 0.0
    total_compression = 0.0
    total_cc = 0.0
    for row in rows:
        model_name = row["model"]
        rates = get_rates(model_name)
        input_rate = rates["input"]
        cached_rate = rates["cached"]
        output_rate = rates["output"]

        req_count = row["requests"]
        input_tok = row["input_tokens"]          # post-compression tokens sent
        output_tok = row["output_tokens"]
        cache_read = row["cache_read_tokens"]
        compressed_tok = row["compressed_tokens"]  # proxy-attributed only (see SQL)
        proxy_managed_cr = (
            row["proxy_managed_cache_read"]
            if "proxy_managed_cache_read" in row.keys()
            else 0
        )
        client_managed_cr = max(0, cache_read - proxy_managed_cr)

        # "Without TokenPak": compression undone (sent at full input rate) and
        # proxy-managed cache reads billed as full-price input; client-managed
        # cache stays cached (the client would have cached regardless).
        raw_input = input_tok + compressed_tok
        baseline_input = raw_input + proxy_managed_cr
        without_cost = (
            (baseline_input / 1_000_000) * input_rate
            + (client_managed_cr / 1_000_000) * cached_rate
            + (output_tok / 1_000_000) * output_rate
        )
        with_cost = (
            (input_tok / 1_000_000) * input_rate
            + (cache_read / 1_000_000) * cached_rate
            + (output_tok / 1_000_000) * output_rate
        )
        saved = without_cost - with_cost
        cache_saving = (proxy_managed_cr / 1_000_000) * (input_rate - cached_rate)
        compression_saving = (compressed_tok / 1_000_000) * input_rate
        cc_saving = (client_managed_cr / 1_000_000) * (input_rate - cached_rate)
        pct = (saved / without_cost * 100) if without_cost > 0 else 0.0
        total_input_handled = cache_read + input_tok
        cache_hit_rate = (
            (cache_read / total_input_handled * 100) if total_input_handled > 0 else 0.0
        )

        result.models.append(
            {
                "model": model_name,
                "requests": req_count,
                "without_cost": round(without_cost, 2),
                "with_cost": round(with_cost, 2),
                "saved": round(saved, 2),
                "savings_pct": round(pct, 1),
                "cache_hit_rate": round(cache_hit_rate, 1),
                "cache_savings": round(cache_saving, 2),
                "compression_savings": round(compression_saving, 2),
                "claude_code_cache_savings": round(cc_saving, 2),
                "input_tokens": input_tok,
                "output_tokens": output_tok,
                "cache_read_tokens": cache_read,
                "proxy_managed_cache_read": proxy_managed_cr,
                "client_managed_cache_read": client_managed_cr,
                "compressed_tokens": compressed_tok,
            }
        )
        result.requests += req_count
        result.saved_tokens += int(compressed_tok)
        total_without += without_cost
        total_with += with_cost
        total_cache += cache_saving
        total_compression += compression_saving
        total_cc += cc_saving

    total_saved = total_without - total_with
    result.baseline_cost = total_without
    result.actual_cost = total_with
    result.saved_cost = total_saved
    result.savings_pct = (total_saved / total_without * 100) if total_without > 0 else 0.0
    result.cache_savings = total_cache
    result.compression_savings = total_compression
    result.routing_savings = max(0.0, total_saved - total_cache - total_compression)
    result.claude_code_cache_savings = total_cc
    return result


# NOTE: ``compute_savings`` / ``SavingsResult`` are intentionally NOT exported in
# ``__all__`` below. Consumers import them by explicit name; keeping them out of
# ``__all__`` leaves the Std 21 public-API snapshot unchanged (no regen needed).
__all__ = [
    "parse_openai_usage",
    "parse_anthropic_usage",
    "aggregate_attributions",
    "format_savings_by_source",
    "SourceSummary",
    "attribution_to_row",
]
