"""Rolling/cumulative spend caps — supplements the per-session cap.

The per-session cap (`session_block_cost_usd`) catches a single session
that runs away. The 2026-05-15 incident proved that 64 well-bounded
sub-cap sessions can still cumulate to $566+ over 8 hours because the
session cap doesn't see cross-session totals. This module adds rolling
cumulative caps:

    per-agent  : max cost / tokens / cache_read per hour
    per-fleet  : max cost / tokens / cache_read per hour

If any cap would be exceeded by the projected cost of THIS request, the
guard returns a block with error.type=tokenpak_spend_guard_rolling_cap_blocked.

Design notes:

- Reads existing monitor.db columns only (timestamp, input_tokens,
  output_tokens, cache_read_tokens, estimated_cost, session_id). No
  schema change.
- Agent attribution comes from the X-Tokenpak-Agent request header set
  by agent-claude-worker.sh. Sessions-without-header are bucketed to
  "unknown" and only the aggregate cap restrains them.
- Session→agent mapping is maintained in-memory as requests flow.
  After proxy restart, the mapping resets — that's degraded but safe
  (fail-open: under-count briefly, never over-block).
- A 30-second result cache limits monitor.db query load when many
  requests arrive in burst.
- Existing per-session cap behavior is UNCHANGED.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from tokenpak import _paths

_log = logging.getLogger(__name__)

# In-memory session→agent mapping. Populated as the proxy sees requests
# (orchestrator calls record_session_agent at evaluate-time). Cleared
# only on proxy restart. Bounded growth — sessions are TTL'd at the
# window boundary on each lookup.
_SESSION_AGENT_LOCK = threading.Lock()
_SESSION_AGENT: dict[str, tuple[str, float]] = {}  # session_id → (agent_id, last_seen_epoch)

# Result cache for rolling-usage queries — 30s TTL.
_USAGE_CACHE_LOCK = threading.Lock()
_USAGE_CACHE: dict[str, tuple[float, dict]] = {}  # key → (expires_at, usage_dict)
_USAGE_CACHE_TTL_SEC = 30.0


@dataclass
class RollingCapsConfig:
    """Rolling-cap settings. All fields editable via SpendGuardConfig."""

    enabled: bool = True
    window_seconds: int = 3600

    # Per-agent caps
    per_agent_max_cost_usd: float = 20.0
    per_agent_max_tokens_total: int = 5_000_000
    per_agent_max_cache_read_tokens: int = 4_000_000

    # Aggregate caps across all agents
    per_fleet_max_cost_usd: float = 60.0
    per_fleet_max_tokens_total: int = 15_000_000
    per_fleet_max_cache_read_tokens: int = 12_000_000


@dataclass
class CapBreach:
    """A rolling-cap evaluation result indicating the request must block."""

    cap_dimension: str  # e.g. "per_agent_cost_usd", "per_fleet_cache_read_tokens"
    agent_id: str
    window_seconds: int
    used: float  # current usage (cost in USD or tokens as int)
    cap: float  # configured cap
    projected_add: float  # what THIS request would add
    retry_after_seconds: int  # seconds until enough usage ages out
    # Standard 29 §13.5 surfacing — spend the managed denominator excluded
    # (non-managed traffic) and the managed-but-unattributed sub-bucket. Both
    # default 0.0 so existing constructors / tests are unaffected.
    excluded_observed_spend: float = 0.0
    managed_unattributed_spend: float = 0.0


def record_session_agent(session_id: str, agent_id: str) -> None:
    """Record the (session_id → agent_id) mapping for future per-agent lookup.

    Called once per request at proxy entry (after the existing
    session_id resolution + header parse). No-op for empty inputs.
    """
    if not session_id or not agent_id:
        return
    with _SESSION_AGENT_LOCK:
        _SESSION_AGENT[session_id] = (agent_id.lower(), time.time())


def _warn_degraded(message: str) -> None:
    _log.warning(message)
    print(f"tokenpak: WARN {message}", file=sys.stderr)


def _blank_usage(*, degraded_reason: Optional[str] = None) -> dict:
    return {
        "agent_cost_usd": 0.0,
        "agent_tokens_total": 0,
        "agent_cache_read_tokens": 0,
        "fleet_cost_usd": 0.0,
        "fleet_tokens_total": 0,
        "fleet_cache_read_tokens": 0,
        "managed_unattributed_cost_usd": 0.0,
        "excluded_observed_spend_usd": 0.0,
        "degraded": degraded_reason is not None,
        "degraded_reason": degraded_reason,
    }


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """True iff ``table`` has ``column`` (Standard 29 §13 migration guard).

    A pre-§13 monitor.db lacks ``request_class``; callers fall back to the
    conservative count-all-rows behaviour rather than fail-open to zero.
    """
    try:
        return any(row[1] == column for row in conn.execute(f"PRAGMA table_info({table})"))
    except Exception:
        return False


def _cache_usage(cache_key: str, usage: dict, now: float) -> dict:
    with _USAGE_CACHE_LOCK:
        _USAGE_CACHE[cache_key] = (now + _USAGE_CACHE_TTL_SEC, usage)
    return usage


def _path(monitor_db_path: Optional[str]) -> Optional[Path]:
    if monitor_db_path:
        return Path(os.path.expanduser(monitor_db_path))
    return _paths.monitor_db(mode="read")


def _missing_db_reason() -> str:
    try:
        candidates = _paths.monitor_db_candidates()
    except Exception:
        return "monitor_db_unresolved"
    return (
        "monitor_db_invalid"
        if any(candidate.get("exists") for candidate in candidates)
        else "monitor_db_missing"
    )


def _get_agents_for_window(window_seconds: int) -> dict[str, list[str]]:
    """Return {agent_id: [session_id, ...]} for sessions seen in the window.

    Sessions without a recorded mapping (e.g. pre-restart) are excluded.
    """
    cutoff = time.time() - float(window_seconds)
    out: dict[str, list[str]] = {}
    with _SESSION_AGENT_LOCK:
        for sid, (agent, last_seen) in list(_SESSION_AGENT.items()):
            if last_seen < cutoff:
                # Prune stale entries
                _SESSION_AGENT.pop(sid, None)
                continue
            out.setdefault(agent, []).append(sid)
    return out


def compute_rolling_usage(
    agent_id: str,
    window_seconds: int,
    *,
    monitor_db_path: Optional[str] = None,
) -> dict:
    """Compute rolling-window usage for one agent plus aggregate traffic.

    Returns:
        {
          "agent_cost_usd": float,
          "agent_tokens_total": int,
          "agent_cache_read_tokens": int,
          "fleet_cost_usd": float,
          "fleet_tokens_total": int,
          "fleet_cache_read_tokens": int,
        }

    Cached for 30 seconds keyed by (agent_id, window_seconds, db_path).
    Returns all-zero on any failure (fail open).
    """
    p = _path(monitor_db_path)
    db_path = str(p) if p is not None else "<unresolved-monitor-db>"
    cache_key = f"{agent_id}|{window_seconds}|{db_path}"

    now = time.time()
    with _USAGE_CACHE_LOCK:
        cached = _USAGE_CACHE.get(cache_key)
        if cached and cached[0] > now:
            return cached[1]

    if p is None:
        reason = _missing_db_reason()
        _warn_degraded(
            "rolling caps degraded: no valid monitor.db resolved via "
            f"tokenpak._paths.monitor_db(mode='read') ({reason}); returning unknown/zero usage"
        )
        return _cache_usage(cache_key, _blank_usage(degraded_reason=reason), now)
    if not p.exists():
        _warn_degraded(
            f"rolling caps degraded: monitor.db not found at {p}; returning unknown/zero usage"
        )
        return _cache_usage(
            cache_key,
            _blank_usage(degraded_reason="monitor_db_missing"),
            now,
        )

    cutoff_iso = time.strftime(
        "%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - float(window_seconds))
    )
    try:
        conn = sqlite3.connect(str(p), timeout=2.0)
        # Standard 29 §13.5 — managed-cap denominators count ONLY request_class
        # = 'managed' traffic. raw_claude_observed / external_untagged rows (and
        # pre-§13 rows, which default to external_untagged) live in their own
        # buckets and MUST NOT inflate any managed-agent / fleet denominator. On
        # a pre-§13 DB without the column, fall back to counting all rows (the
        # conservative, never-under-block posture).
        managed = _has_column(conn, "requests", "request_class")
        managed_clause = " AND request_class = 'managed'" if managed else ""
        # Fleet-wide MANAGED totals.
        # tokens_total = input + output (cache_read EXCLUDED
        # 2026-05-15: Anthropic bills cache_read ~90% cheaper, so cache_read
        # inflation should not trip the rolling tokens cap. cache_read is
        # still recorded for observability + its own dedicated cap.
        row = conn.execute(
            f"""SELECT COALESCE(SUM(estimated_cost), 0.0),
                      COALESCE(SUM(input_tokens), 0) + COALESCE(SUM(output_tokens), 0),
                      COALESCE(SUM(cache_read_tokens), 0)
               FROM requests
               WHERE timestamp >= ?{managed_clause}""",
            (cutoff_iso,),
        ).fetchone()
        fleet_cost, fleet_tokens, fleet_cache_read = float(row[0]), int(row[1]), int(row[2])

        # §13.5 surfacing — spend the managed denominator deliberately ignored
        # (non-managed traffic), and the managed-but-unattributed sub-bucket
        # (managed traffic with no agent attribution: counts toward the fleet
        # aggregate above, never toward a per-agent denominator below).
        excluded_observed_spend = 0.0
        managed_unattributed_cost = 0.0
        if managed:
            total_cost = conn.execute(
                "SELECT COALESCE(SUM(estimated_cost), 0.0) FROM requests WHERE timestamp >= ?",
                (cutoff_iso,),
            ).fetchone()[0]
            excluded_observed_spend = max(0.0, float(total_cost) - fleet_cost)
            attributed = [
                s
                for sessions in _get_agents_for_window(window_seconds).values()
                for s in sessions
            ]
            if attributed:
                ph = ",".join("?" for _ in attributed)
                managed_unattributed_cost = float(
                    conn.execute(
                        f"""SELECT COALESCE(SUM(estimated_cost), 0.0)
                            FROM requests
                            WHERE timestamp >= ?{managed_clause}
                              AND COALESCE(session_id, '') NOT IN ({ph})""",
                        (cutoff_iso, *attributed),
                    ).fetchone()[0]
                )
            else:
                # No mapped sessions in-window → all managed spend is unattributed.
                managed_unattributed_cost = fleet_cost

        # Per-agent MANAGED totals — restrict to sessions the proxy has mapped
        # to this agent. Managed-without-agent rows have no mapping and so are
        # excluded here (per §13.5 they belong to the fleet aggregate +
        # managed-unattributed sub-bucket, never a per-agent denominator).
        agent_cost = 0.0
        agent_tokens = 0
        agent_cache_read = 0
        if agent_id:
            mapping = _get_agents_for_window(window_seconds)
            sessions = mapping.get(agent_id.lower(), [])
            if sessions:
                placeholders = ",".join("?" for _ in sessions)
                row2 = conn.execute(
                    f"""SELECT COALESCE(SUM(estimated_cost), 0.0),
                              COALESCE(SUM(input_tokens), 0) + COALESCE(SUM(output_tokens), 0),
                              COALESCE(SUM(cache_read_tokens), 0)
                       FROM requests
                       WHERE timestamp >= ?{managed_clause}
                         AND session_id IN ({placeholders})""",
                    (cutoff_iso, *sessions),
                ).fetchone()
                agent_cost, agent_tokens, agent_cache_read = (
                    float(row2[0]),
                    int(row2[1]),
                    int(row2[2]),
                )
        conn.close()
        usage = {
            "agent_cost_usd": agent_cost,
            "agent_tokens_total": agent_tokens,
            "agent_cache_read_tokens": agent_cache_read,
            "fleet_cost_usd": fleet_cost,
            "fleet_tokens_total": fleet_tokens,
            "fleet_cache_read_tokens": fleet_cache_read,
            "managed_unattributed_cost_usd": managed_unattributed_cost,
            "excluded_observed_spend_usd": excluded_observed_spend,
            "degraded": False,
            "degraded_reason": None,
        }
        with _USAGE_CACHE_LOCK:
            _USAGE_CACHE[cache_key] = (now + _USAGE_CACHE_TTL_SEC, usage)
        return usage
    except sqlite3.OperationalError as e:
        _warn_degraded(
            f"rolling caps degraded: monitor.db query failed for {p}: {e}; "
            "returning unknown/zero usage"
        )
        return _cache_usage(
            cache_key,
            _blank_usage(degraded_reason="monitor_db_query_failed"),
            now,
        )
    except Exception as e:
        _warn_degraded(
            f"rolling caps degraded: unexpected monitor.db error for {p}: {e}; "
            "returning unknown/zero usage"
        )
        return _cache_usage(
            cache_key,
            _blank_usage(degraded_reason="monitor_db_unexpected_error"),
            now,
        )


def check_rolling_caps(
    agent_id: str,
    projected_cost_usd: float,
    projected_input_tokens: int,
    projected_output_tokens: int,
    projected_cache_read_tokens: int,
    config: RollingCapsConfig,
    *,
    monitor_db_path: Optional[str] = None,
) -> Optional[CapBreach]:
    """Evaluate all configured rolling caps; return the FIRST breach or None.

    The check order (matches packet doc): per-agent cost → per-agent
    tokens → per-agent cache_read → per-fleet cost → per-fleet tokens
    → per-fleet cache_read. First breach wins so the error message
    pinpoints the tightest constraint.

    Returns None when:
        - Rolling caps are disabled
        - Usage is below all configured caps (with projected_add included)
        - Any computation error (fail-open per Standard 29 §9.8)
    """
    if not config.enabled:
        return None
    usage = compute_rolling_usage(agent_id, config.window_seconds, monitor_db_path=monitor_db_path)
    # §13.5 surfacing — stamp every breach this evaluation produces with the
    # excluded / managed-unattributed spend so the 402 copy can show it.
    _excl = float(usage.get("excluded_observed_spend_usd", 0.0) or 0.0)
    _unattr = float(usage.get("managed_unattributed_cost_usd", 0.0) or 0.0)

    def _breach(**kw) -> CapBreach:
        return CapBreach(
            excluded_observed_spend=_excl,
            managed_unattributed_spend=_unattr,
            **kw,
        )

    # tokens_total = input + output only (cache_read EXCLUDED
    # 2026-05-15: cache_read is ~90% cheaper and inflates the count without
    # reflecting real cost. cache_read keeps its own dedicated cap dimension.
    projected_tokens_total = int(projected_input_tokens) + int(projected_output_tokens)

    def retry_after(cost_used: float, tokens_used: float, cap: float) -> int:
        # Coarse heuristic: time until the oldest in-window request ages
        # out. We don't have per-row aging info here; return a flat 30
        # min for now, the operator can re-try after.
        return 1800

    # Per-agent — only when agent_id is known
    if agent_id:
        a_cost = usage["agent_cost_usd"]
        if (
            config.per_agent_max_cost_usd > 0
            and a_cost + projected_cost_usd > config.per_agent_max_cost_usd
        ):
            return _breach(
                cap_dimension="per_agent_cost_usd",
                agent_id=agent_id,
                window_seconds=config.window_seconds,
                used=a_cost,
                cap=config.per_agent_max_cost_usd,
                projected_add=projected_cost_usd,
                retry_after_seconds=retry_after(a_cost, 0, config.per_agent_max_cost_usd),
            )
        a_tok = usage["agent_tokens_total"]
        if (
            config.per_agent_max_tokens_total > 0
            and a_tok + projected_tokens_total > config.per_agent_max_tokens_total
        ):
            return _breach(
                cap_dimension="per_agent_tokens_total",
                agent_id=agent_id,
                window_seconds=config.window_seconds,
                used=float(a_tok),
                cap=float(config.per_agent_max_tokens_total),
                projected_add=float(projected_tokens_total),
                retry_after_seconds=retry_after(0, a_tok, config.per_agent_max_tokens_total),
            )
        a_cr = usage["agent_cache_read_tokens"]
        if (
            config.per_agent_max_cache_read_tokens > 0
            and a_cr + projected_cache_read_tokens > config.per_agent_max_cache_read_tokens
        ):
            return _breach(
                cap_dimension="per_agent_cache_read_tokens",
                agent_id=agent_id,
                window_seconds=config.window_seconds,
                used=float(a_cr),
                cap=float(config.per_agent_max_cache_read_tokens),
                projected_add=float(projected_cache_read_tokens),
                retry_after_seconds=retry_after(0, a_cr, config.per_agent_max_cache_read_tokens),
            )

    # Aggregate cap applies whether or not agent is known
    f_cost = usage["fleet_cost_usd"]
    if (
        config.per_fleet_max_cost_usd > 0
        and f_cost + projected_cost_usd > config.per_fleet_max_cost_usd
    ):
        return _breach(
            cap_dimension="per_fleet_cost_usd",
            agent_id=agent_id or "unknown",
            window_seconds=config.window_seconds,
            used=f_cost,
            cap=config.per_fleet_max_cost_usd,
            projected_add=projected_cost_usd,
            retry_after_seconds=retry_after(f_cost, 0, config.per_fleet_max_cost_usd),
        )
    f_tok = usage["fleet_tokens_total"]
    if (
        config.per_fleet_max_tokens_total > 0
        and f_tok + projected_tokens_total > config.per_fleet_max_tokens_total
    ):
        return _breach(
            cap_dimension="per_fleet_tokens_total",
            agent_id=agent_id or "unknown",
            window_seconds=config.window_seconds,
            used=float(f_tok),
            cap=float(config.per_fleet_max_tokens_total),
            projected_add=float(projected_tokens_total),
            retry_after_seconds=retry_after(0, f_tok, config.per_fleet_max_tokens_total),
        )
    f_cr = usage["fleet_cache_read_tokens"]
    if (
        config.per_fleet_max_cache_read_tokens > 0
        and f_cr + projected_cache_read_tokens > config.per_fleet_max_cache_read_tokens
    ):
        return _breach(
            cap_dimension="per_fleet_cache_read_tokens",
            agent_id=agent_id or "unknown",
            window_seconds=config.window_seconds,
            used=float(f_cr),
            cap=float(config.per_fleet_max_cache_read_tokens),
            projected_add=float(projected_cache_read_tokens),
            retry_after_seconds=retry_after(0, f_cr, config.per_fleet_max_cache_read_tokens),
        )
    return None


def reset_caches_for_testing() -> None:
    """Test-only — clear in-memory caches between test runs."""
    with _SESSION_AGENT_LOCK:
        _SESSION_AGENT.clear()
    with _USAGE_CACHE_LOCK:
        _USAGE_CACHE.clear()
