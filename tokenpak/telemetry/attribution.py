"""attribution.py — Agent/skill attribution tracking for TokenPak.

Tracks which agents and skills drive savings through the proxy.
Uses request headers and metadata to attribute cost savings.
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

ATTRIBUTION_PATH = Path.home() / ".tokenpak" / "attribution_history.json"


@dataclass
class AttributionRecord:
    """Single request attribution."""

    request_id: str = ""
    timestamp: float = 0.0
    source: str = "unknown"
    model: str = ""
    tokens_saved: int = 0
    cost_saved: float = 0.0
    cache_hit: bool = False
    compression_pct: float = 0.0

    def to_dict(self) -> dict:
        """Convert attribution record to dictionary for JSON serialization.

        Returns:
            dict: Record with all metrics including tokens/cost saved and cache hit status
        """
        return {
            "request_id": self.request_id,
            "timestamp": self.timestamp,
            "source": self.source,
            "model": self.model,
            "tokens_saved": self.tokens_saved,
            "cost_saved": round(self.cost_saved, 6),
            "cache_hit": self.cache_hit,
            "compression_pct": round(self.compression_pct, 2),
        }


def detect_source(
    headers: Optional[Dict[str, str]] = None,
    client_ip: str = "",
    user_agent: str = "",
) -> str:
    """Infer request source from headers and metadata.

    Priority:
    1. X-TokenPak-Source header (explicit)
    2. X-TokenPak-Skill header
    3. X-TokenPak-Session header (agent name)
    4. User-Agent hints
    5. Client IP → hostname mapping
    6. "unknown"
    """
    headers = headers or {}

    # 1. Explicit source
    src = headers.get("X-TokenPak-Source", "")
    if src:
        return src

    # 2. Skill header
    skill = headers.get("X-TokenPak-Skill", "")
    if skill:
        return f"skill:{skill}"

    # 3. Session header (often contains agent name)
    session = headers.get("X-TokenPak-Session", "")
    if session:
        # Extract agent name from session patterns like "alpha-main", "beta-heartbeat"
        lower = session.lower()
        for agent in ["alpha", "beta", "gamma"]:
            if agent in lower:
                return f"agent-{agent}"
        return f"session:{session[:30]}"

    # 4. User-Agent hints
    ua = user_agent or headers.get("User-Agent", "")
    if ua:
        ua_lower = ua.lower()
        if "tokenpak" in ua_lower:
            return "tokenpak"
        if "codex" in ua_lower or "coding" in ua_lower:
            return "coding-agent"

    # 5. IP mapping
    ip_map = {
        "127.0.0.1": "localhost",
    }
    if client_ip in ip_map:
        return ip_map[client_ip]

    return "unknown"


class AttributionTracker:
    """Track and aggregate attribution data."""

    def __init__(self, max_records: int = 5000):
        self._records: List[AttributionRecord] = []
        self._max = max_records

    def record(self, rec: AttributionRecord) -> None:
        """Add an attribution record to the tracker.

        Args:
            rec: Attribution record to track. Auto-timestamps if not set.
        """
        if rec.timestamp <= 0:
            rec.timestamp = time.time()
        self._records.append(rec)
        if len(self._records) > self._max:
            self._records = self._records[-self._max :]

    @property
    def records(self) -> List[AttributionRecord]:
        """Get all tracked attribution records.

        Returns:
            list: Copy of internal records list
        """
        return list(self._records)

    def rollup_by_source(self, since: Optional[float] = None) -> Dict[str, dict]:
        """Aggregate stats by source."""
        filtered = self._records
        if since:
            filtered = [r for r in filtered if r.timestamp >= since]

        groups: Dict[str, dict] = defaultdict(
            lambda: {
                "requests": 0,
                "tokens_saved": 0,
                "cost_saved": 0.0,
                "cache_hits": 0,
                "models": defaultdict(int),
            }
        )

        for r in filtered:
            g = groups[r.source]
            g["requests"] += 1
            g["tokens_saved"] += r.tokens_saved
            g["cost_saved"] += r.cost_saved
            if r.cache_hit:
                g["cache_hits"] += 1
            g["models"][r.model] += 1

        # Compute derived fields
        result = {}
        for src, g in groups.items():
            total = g["requests"]
            top_model = max(g["models"].items(), key=lambda x: x[1])[0] if g["models"] else ""
            result[src] = {
                "requests": total,
                "tokens_saved": g["tokens_saved"],
                "cost_saved": round(g["cost_saved"], 4),
                "cache_hit_rate": round(g["cache_hits"] / total, 2) if total else 0,
                "top_model": top_model,
            }

        return dict(sorted(result.items(), key=lambda x: -x[1]["cost_saved"]))

    def rollup_by_model(self, since: Optional[float] = None) -> Dict[str, dict]:
        """Aggregate stats by model."""
        filtered = self._records
        if since:
            filtered = [r for r in filtered if r.timestamp >= since]

        groups: Dict[str, dict] = defaultdict(
            lambda: {
                "requests": 0,
                "tokens_saved": 0,
                "cost_saved": 0.0,
            }
        )

        for r in filtered:
            g = groups[r.model]
            g["requests"] += 1
            g["tokens_saved"] += r.tokens_saved
            g["cost_saved"] += r.cost_saved

        return dict(sorted(groups.items(), key=lambda x: -x[1]["cost_saved"]))

    def leakage_pct(self, since: Optional[float] = None) -> float:
        """Percentage of requests with unknown source."""
        filtered = self._records
        if since:
            filtered = [r for r in filtered if r.timestamp >= since]
        if not filtered:
            return 0.0
        unknown = sum(1 for r in filtered if r.source == "unknown")
        return round(unknown / len(filtered) * 100, 1)

    def save(self, path: Optional[Path] = None) -> None:
        """Persist attribution data."""
        p = path or ATTRIBUTION_PATH
        p.parent.mkdir(parents=True, exist_ok=True)
        data = [r.to_dict() for r in self._records]
        p.write_text(json.dumps(data, indent=2))

    def load(self, path: Optional[Path] = None) -> None:
        """Load attribution data from file."""
        p = path or ATTRIBUTION_PATH
        if not p.exists():
            return
        try:
            data = json.loads(p.read_text())
            for d in data:
                self._records.append(
                    AttributionRecord(
                        **{
                            k: v
                            for k, v in d.items()
                            if k in AttributionRecord.__dataclass_fields__
                        }
                    )
                )
        except (json.JSONDecodeError, TypeError):
            pass


# Agent emoji map
AGENT_EMOJI = {
    "sue": "💻",
    "trix": "🐰",
    "cali": "✨",
    "unknown": "??",
    "localhost": "🏠",
}


def get_attribution_summary_from_db(db_path, days: int = 7) -> str:
    """Format attribution report by reading monitor.db attribution_source column directly."""
    import sqlite3
    from datetime import datetime, timedelta

    since = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
    try:
        conn = sqlite3.connect(str(db_path), timeout=2)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            """SELECT
                COALESCE(attribution_source, 'unknown') AS source,
                COUNT(*) AS requests,
                COUNT(CASE WHEN cache_read_tokens > 0 THEN 1 END) AS cache_hits
               FROM requests
               WHERE timestamp >= ? AND status_code < 400
               GROUP BY COALESCE(attribution_source, 'unknown')
               ORDER BY COUNT(*) DESC""",
            (since,),
        )
        by_source = [dict(r) for r in cur.fetchall()]
        cur.execute(
            """SELECT model, COUNT(*) AS requests
               FROM requests
               WHERE timestamp >= ? AND status_code < 400
               GROUP BY model
               ORDER BY COUNT(*) DESC
               LIMIT 5""",
            (since,),
        )
        by_model = [dict(r) for r in cur.fetchall()]
        cur.execute(
            """SELECT
                COUNT(*) AS total,
                COUNT(CASE WHEN COALESCE(attribution_source,'unknown') = 'unknown' THEN 1 END)
                    AS unknown_count
               FROM requests
               WHERE timestamp >= ? AND status_code < 400""",
            (since,),
        )
        totals = dict(cur.fetchone())
        conn.close()
    except Exception:
        return ""

    if not by_source:
        return ""

    total_requests = totals.get("total", 0) or 1
    unknown_count = totals.get("unknown_count", 0) or 0
    leakage_pct = unknown_count / total_requests * 100

    lines = [
        f"TokenPak Attribution — Last {days} Days",
        "───────────────────────────────────",
        f"  Total requests: {total_requests:,}",
        "",
        "Agent Breakdown:",
    ]
    for row in by_source:
        src = row["source"]
        reqs = row["requests"]
        cache_rate = (row["cache_hits"] / reqs * 100) if reqs else 0
        pct = reqs / total_requests * 100
        emoji = "??"
        for key, em in AGENT_EMOJI.items():
            if key in src.lower():
                emoji = em
                break
        lines.append(
            f"  {emoji} {src:<22} {reqs:>6} req ({pct:.0f}%)  cache:{cache_rate:.0f}%"
        )

    lines.append("")
    lines.append("Top Models (by requests):")
    for row in by_model:
        model = row["model"] or "unknown"
        pct = row["requests"] / total_requests * 100
        lines.append(f"  {model:<24} {row['requests']:>6} req ({pct:.0f}%)")

    if leakage_pct > 5:
        lines.append("")
        lines.append(
            f"  ⚠️ HIGH LEAKAGE: {leakage_pct:.0f}% of requests lack source attribution"
        )
        lines.append("     → Add X-TokenPak-Source header to identify origin")

    return "\n".join(lines)


def format_attribution(tracker: AttributionTracker, days: int = 7) -> str:
    """Format human-readable attribution report."""
    since = time.time() - (days * 86400)
    by_source = tracker.rollup_by_source(since=since)
    by_model = tracker.rollup_by_model(since=since)

    if not by_source:
        return (
            "No attribution data found.\nRun TokenPak with requests to see attribution breakdown."
        )

    total_saved = sum(v["cost_saved"] for v in by_source.values())

    lines = [
        f"TokenPak Attribution — Last {days} Days",
        "───────────────────────────────────",
        "",
        "Agent Breakdown:",
    ]

    for src, stats in by_source.items():
        pct = (stats["cost_saved"] / total_saved * 100) if total_saved > 0 else 0
        # Find emoji
        emoji = "??"
        for key, em in AGENT_EMOJI.items():
            if key in src.lower():
                emoji = em
                break
        lines.append(f"  {emoji} {src:<22} ${stats['cost_saved']:>10.2f} ({pct:.0f}%)")

    lines.append("")
    lines.append("Top Models (by savings):")
    for model, stats in list(by_model.items())[:5]:
        pct = (stats["cost_saved"] / total_saved * 100) if total_saved > 0 else 0
        lines.append(f"  {model:<24} ${stats['cost_saved']:>10.2f} ({pct:.0f}%)")

    # Leakage warning
    leakage = tracker.leakage_pct(since=since)
    if leakage > 5:
        lines.append("")
        lines.append(f"  ⚠️ HIGH LEAKAGE: {leakage:.0f}% of requests lack source attribution")
        lines.append("     → Add X-TokenPak-Source header to identify origin")

    return "\n".join(lines)
