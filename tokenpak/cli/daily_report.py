# SPDX-License-Identifier: Apache-2.0
"""TokenPak Daily Savings Report Generator

Generates formatted daily summaries for TokenPak usage and savings.
Suitable for automated reporting via CLI, cron, or messaging.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Literal, TypedDict, cast


@dataclass
class ModelCompressionRow:
    """Per-model compression row for the daily report."""

    model: str
    request_count: int
    avg_compression_ratio: float  # final/raw; lower = more compression
    tokens_saved: int
    savings_amount: float


@dataclass
class DailySavingsData:
    """Daily savings summary data."""

    timestamp: str  # ISO format
    requests: int
    savings_amount: float
    savings_percent: float
    cache_hit_rate: float
    compression_percent: float
    top_model: str
    top_model_savings: float
    uptime_hours: int | Literal["unknown"]
    uptime_minutes: int
    errors: int
    estimated_monthly_rate: float
    model_compression: list[ModelCompressionRow] = field(default_factory=list)


class SavingsSummary(TypedDict):
    """Fields consumed from a one-day telemetry savings report."""

    total_cost: float
    estimated_without_compression: float
    savings_amount: float
    savings_pct: float
    cache_hit_rate: float


def _proxy_get(path: str, port: int | None = None) -> dict[str, object] | None:
    """Fetch JSON from running proxy. Returns None if unreachable."""
    import urllib.request as _urlreq

    port = port or int(os.environ.get("TOKENPAK_PORT", "8766"))
    try:
        resp = _urlreq.urlopen(f"http://127.0.0.1:{port}{path}", timeout=2)
        payload = json.loads(resp.read())
        return cast(dict[str, object], payload) if isinstance(payload, dict) else None
    except Exception:
        return None


def _get_model_compression_breakdown() -> list[ModelCompressionRow]:
    """Fetch per-model compression breakdown from telemetry. Returns [] on error."""
    try:
        from tokenpak.telemetry.query_dsl import get_model_compression_breakdown

        rows = get_model_compression_breakdown(days=1)
        return [
            ModelCompressionRow(
                model=r.model,
                request_count=r.request_count,
                avg_compression_ratio=r.avg_compression_ratio,
                tokens_saved=r.tokens_saved,
                savings_amount=r.savings_amount,
            )
            for r in rows
        ]
    except Exception:
        return []


def _get_savings_report() -> SavingsSummary:
    """Get historical savings data from telemetry."""
    try:
        from tokenpak.telemetry.query_dsl import get_savings_report

        report = get_savings_report(days=1)
        return {
            "total_cost": report.total_cost,
            "estimated_without_compression": report.estimated_without_compression,
            "savings_amount": report.savings_amount,
            "savings_pct": report.savings_pct,
            "cache_hit_rate": report.cache_hit_rate,
        }
    except Exception:
        return {
            "total_cost": 0.0,
            "estimated_without_compression": 0.0,
            "savings_amount": 0.0,
            "savings_pct": 0.0,
            "cache_hit_rate": 0.0,
        }


def _calculate_data() -> DailySavingsData:
    """Collect live proxy stats and calculate daily summary."""
    health = _proxy_get("/health") or {}
    stats = _proxy_get("/stats") or {}
    cache = _proxy_get("/cache-stats") or {}
    telemetry = _get_savings_report()

    # Extract stats
    health_stats = health.get("stats", {})
    start_time = health_stats.get("start_time") if isinstance(health_stats, dict) else None
    if start_time is None:
        uptime_h: int | Literal["unknown"] = "unknown"
        uptime_m = 0
    elif isinstance(start_time, (int, float)):
        uptime_s = max(0, time.time() - start_time)
        uptime_h = int(uptime_s // 3600)
        uptime_m = int((uptime_s % 3600) // 60)
    else:
        uptime_h = "unknown"
        uptime_m = 0

    # Requests and errors
    requests_value = stats.get("requests", 0)
    errors_value = stats.get("errors", 0)
    requests = int(requests_value) if isinstance(requests_value, (int, float)) else 0
    errors = int(errors_value) if isinstance(errors_value, (int, float)) else 0

    # Tokens
    input_value = stats.get("input_tokens", 0)
    saved_value = stats.get("saved_tokens", 0)
    input_tokens = float(input_value) if isinstance(input_value, (int, float)) else 0.0
    saved_tokens = float(saved_value) if isinstance(saved_value, (int, float)) else 0.0
    compression_pct = (saved_tokens / input_tokens * 100) if input_tokens > 0 else 0

    # Cache
    hits_value = cache.get("cache_hits", 0)
    misses_value = cache.get("cache_misses", 0)
    cache_hits = float(hits_value) if isinstance(hits_value, (int, float)) else 0.0
    cache_misses = float(misses_value) if isinstance(misses_value, (int, float)) else 0.0
    cache_total = cache_hits + cache_misses
    cache_hit_rate = (cache_hits / cache_total) if cache_total > 0 else 0.0

    # Savings from telemetry
    savings_amount = telemetry.get("savings_amount", 0.0)
    savings_percent = telemetry.get("savings_pct", 0.0)

    # Top model (from telemetry or fallback)
    top_model = "unknown"
    top_model_savings = 0.0
    try:
        from tokenpak.telemetry.query_dsl import get_model_usage

        usage = get_model_usage(days=1)
        if usage:
            # Find model with highest cost
            model_costs = {}
            for u in usage:
                # Estimate cost based on tokens (simplified)
                model_costs[u.model] = u.request_count
            if model_costs:
                top_model = max(model_costs, key=lambda model: model_costs[model])
                top_model_savings = savings_amount  # Proxy: assume savings proportional
    except Exception:
        pass

    # Estimated monthly rate
    if requests > 0 and savings_amount > 0:
        # Rough estimate: daily savings * 30 / time elapsed
        days_running = max(uptime_h / 24, 0.1) if uptime_h != "unknown" else 0.1
        daily_savings = savings_amount / max(days_running, 0.1)
        estimated_monthly = daily_savings * 30
    else:
        estimated_monthly = 0.0

    # Per-model compression breakdown
    model_compression = _get_model_compression_breakdown()

    return DailySavingsData(
        timestamp=datetime.now().isoformat(),
        requests=requests,
        savings_amount=savings_amount,
        savings_percent=savings_percent,
        cache_hit_rate=cache_hit_rate,
        compression_percent=compression_pct,
        top_model=top_model,
        top_model_savings=top_model_savings,
        uptime_hours=uptime_h,
        uptime_minutes=uptime_m,
        errors=errors,
        estimated_monthly_rate=estimated_monthly,
        model_compression=model_compression,
    )


def _format_compression_table_terminal(rows: list[ModelCompressionRow]) -> list[str]:
    """Format per-model compression breakdown as terminal lines."""
    if not rows:
        return ["  (no per-model compression data)"]
    lines = [
        "",
        "  Per-Model Compression Breakdown:",
        f"  {'Model':<30} {'Reqs':>6} {'Ratio':>6} {'Saved Tok':>10} {'Saved $':>9}",
        "  " + "─" * 65,
    ]
    for r in rows:
        ratio_pct = (
            f"{(1 - r.avg_compression_ratio) * 100:.1f}%"
            if r.avg_compression_ratio < 1.0
            else "0.0%"
        )
        lines.append(
            f"  {r.model:<30} {r.request_count:>6,} {ratio_pct:>6} {r.tokens_saved:>10,} {r.savings_amount:>9.4f}"
        )
    return lines


def _format_terminal(data: DailySavingsData) -> str:
    """Format as terminal-friendly output."""
    lines = [
        "📊 TokenPak Daily Report",
        "─" * 40,
        f"  Date:       {data.timestamp.split('T')[0]}",
        f"  Requests:   {data.requests:,}",
        f"  Saved:      ${data.savings_amount:.2f} ({data.savings_percent:.1f}%)",
        f"  Cache Hit:  {data.cache_hit_rate * 100:.0f}%",
        f"  Compression: {data.compression_percent:.1f}%",
        f"  Top Model:  {data.top_model}",
        f"  Uptime:     {data.uptime_hours}h {data.uptime_minutes:02d}m",
        f"  Errors:     {data.errors}",
        f"  Monthly Rate: ${data.estimated_monthly_rate:.0f}/mo",
    ]
    lines.extend(_format_compression_table_terminal(data.model_compression or []))
    return "\n".join(lines)


def _format_markdown(data: DailySavingsData) -> str:
    """Format as markdown (suitable for Telegram/messaging)."""
    lines = [
        "## 📊 TokenPak Daily Report",
        "",
        f"**Date:** {data.timestamp.split('T')[0]}",
        "",
        "| Metric | Value |",
        "| ------ | ----- |",
        f"| Requests | {data.requests:,} |",
        f"| Savings | ${data.savings_amount:.2f} ({data.savings_percent:.1f}%) |",
        f"| Cache Hit Rate | {data.cache_hit_rate * 100:.0f}% |",
        f"| Compression | {data.compression_percent:.1f}% |",
        f"| Top Model | {data.top_model} |",
        f"| Uptime | {data.uptime_hours}h {data.uptime_minutes:02d}m |",
        f"| Errors | {data.errors} |",
        f"| Est. Monthly | ${data.estimated_monthly_rate:.0f}/mo |",
    ]
    rows = data.model_compression or []
    if rows:
        lines += [
            "",
            "### Per-Model Compression Breakdown",
            "",
            "| Model | Reqs | Compression | Tokens Saved | Saved $ |",
            "| ----- | ---: | ----------: | -----------: | ------: |",
        ]
        for r in rows:
            ratio_pct = (
                f"{(1 - r.avg_compression_ratio) * 100:.1f}%"
                if r.avg_compression_ratio < 1.0
                else "0.0%"
            )
            lines.append(
                f"| {r.model} | {r.request_count:,} | {ratio_pct} | {r.tokens_saved:,} | ${r.savings_amount:.4f} |"
            )
    else:
        lines += ["", "_No per-model compression data available._"]
    return "\n".join(lines)


def _format_json(data: DailySavingsData) -> dict[str, object]:
    """Format as JSON dict."""
    result = asdict(data)
    # model_compression is a list of ModelCompressionRow dataclasses; asdict handles them
    return cast(dict[str, object], result)


def generate_report(
    format: Literal["terminal", "markdown", "json"] = "terminal",
) -> str | dict[str, object]:
    """Generate daily savings report in specified format.

    Args:
        format: Output format ('terminal', 'markdown', 'json')

    Returns:
        Formatted report string or dict
    """
    data = _calculate_data()

    if format == "markdown":
        return _format_markdown(data)
    elif format == "json":
        return _format_json(data)
    else:  # terminal
        return _format_terminal(data)
