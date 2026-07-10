# SPDX-License-Identifier: Apache-2.0
"""Unified savings report — the single source of truth for "how much did you save".

Before this module, the savings number was computed independently in roughly
four places (``status``, ``doctor``, ``_cli_core``, the deprecated ``savings``
command), each with a different formula and a different data feed. The same
machine in the same minute could therefore report ``$0.00`` (status), a large
dollar figure (savings), and ``100%`` (doctor) — three contradictory answers to
one question.

This module replaces all of those with ONE helper, ``savings_report()``, that:

  * Reads the canonical, attributable feed — the proxy ``monitor.db`` ``requests``
    table, resolved through ``tokenpak._paths.monitor_db()`` (the same resolver
    the proxy writer and every other reader use).
  * Separates savings into **two planes that are NEVER summed into a single
    "TokenPak saved you X"**:

        - ``compression_savings`` — wire-side, TokenPak-EARNED. Only rows whose
          ``cache_origin = 'proxy'`` (TokenPak placed the cache-control markers
          / performed the compression). This is the only number TokenPak may
          claim credit for.
        - ``cache_savings`` — client-attributed. Rows whose ``cache_origin =
          'client'`` (the upstream client, e.g. a coding agent on byte-preserved
          passthrough, placed the cache markers). Real savings — but credited to
          the caller, not to TokenPak.

  * Treats genuinely unattributable rows (``cache_origin`` unknown / NULL /
    sentinel) conservatively: their cache value is credited to the *client*
    plane and surfaced separately in ``unattributable`` so it is never dropped
    and never mis-credited to TokenPak.

  * Exposes a ``db_state`` discriminator so display surfaces can tell the
    difference between "no data — not measured yet" and "measured, here is the
    number". A report with no rows MUST render "not measured yet", never
    ``$0`` / ``0%`` / ``100%`` (those imply a measurement that did not happen).

Every CLI surface (``status``, ``doctor``, ``_cli_core``, ``--json``) routes
through this one helper so they can never disagree again.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any, Optional

# ---------------------------------------------------------------------------
# db_state discriminator
# ---------------------------------------------------------------------------

#: No usable feed: the monitor DB is absent, or present but has zero rows in the
#: requested window. Surfaces MUST render "not measured yet" — never $0/0%/100%.
DB_STATE_NO_DATA = "no_data"

#: Rows present, but none carry a usable ``cache_origin`` attribution. Their
#: cache value is credited to the client plane (conservative; we never
#: over-claim) and reported under ``unattributable``.
DB_STATE_PRESENT_UNATTRIBUTED = "present_unattributed"

#: Rows present and attributed via ``cache_origin`` (proxy / client).
DB_STATE_ATTRIBUTED = "attributed"

# Origin sentinels (the canonical spellings the proxy writer emits).
_ORIGIN_PROXY = "proxy"
_ORIGIN_CLIENT = "client"

# Conservative default pricing (USD per 1M tokens). Used only when a per-model
# rate is unavailable. Anthropic sonnet-class input / cache-read rates.
_DEFAULT_INPUT_RATE = 3.00
_DEFAULT_CACHED_RATE = 0.30


@dataclass
class SavingsPlane:
    """One attribution plane of the savings report.

    The two planes (compression vs cache) are surfaced as separate
    ``SavingsPlane`` objects and are NEVER added together into a single
    headline number.
    """

    label: str = ""
    usd: float = 0.0
    tokens: int = 0
    #: Who this plane is credited to: ``"tokenpak"`` (earned) or
    #: ``"client"`` (observed, credited to the caller).
    credited_to: str = "client"

    def to_json(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "usd": round(self.usd, 6),
            "tokens": int(self.tokens),
            "credited_to": self.credited_to,
        }


@dataclass
class UnifiedSavingsReport:
    """The single savings object every surface renders.

    Hard rule: ``compression_savings`` (TokenPak-earned) and ``cache_savings``
    (client-attributed) are two separate planes. They are exposed separately
    and must never be summed into one "TokenPak saved you X" figure.
    """

    # --- the two planes (never summed) ---
    compression_savings: SavingsPlane = field(
        default_factory=lambda: SavingsPlane(label="compression", credited_to="tokenpak")
    )
    cache_savings: SavingsPlane = field(
        default_factory=lambda: SavingsPlane(label="cache", credited_to="client")
    )

    #: Cache value from rows whose origin could not be attributed. Conservatively
    #: credited to the client plane (folded into ``cache_savings``) but tracked
    #: here too so it is visible and never silently dropped.
    unattributable: SavingsPlane = field(
        default_factory=lambda: SavingsPlane(label="unattributable", credited_to="client")
    )

    # --- context ---
    total_cost: float = 0.0
    request_count: int = 0
    #: Human window label, e.g. "all time", "last 1d", "last 6h".
    window: str = "all time"
    #: One of DB_STATE_NO_DATA / DB_STATE_PRESENT_UNATTRIBUTED / DB_STATE_ATTRIBUTED.
    db_state: str = DB_STATE_NO_DATA
    #: Resolved monitor.db path (or None when no feed was found).
    db_path: Optional[str] = None

    # ------------------------------------------------------------------
    # Honesty predicates — surfaces use these instead of inventing tests.
    # ------------------------------------------------------------------

    @property
    def has_data(self) -> bool:
        """True when there is a measurement to render (rows exist)."""
        return self.db_state != DB_STATE_NO_DATA

    @property
    def is_attributed(self) -> bool:
        return self.db_state == DB_STATE_ATTRIBUTED

    @property
    def tokenpak_earned_usd(self) -> float:
        """The ONLY dollar figure TokenPak may claim credit for.

        This is the compression plane alone. It deliberately does NOT include
        ``cache_savings`` — that is the caller's, not ours.
        """
        return self.compression_savings.usd

    def to_json(self) -> dict[str, Any]:
        """Machine-readable form with stable keys.

        The two planes live under separate keys; there is intentionally NO
        combined "total_savings" key, because summing them would conflate
        TokenPak-earned and client-attributed value.
        """
        return {
            "db_state": self.db_state,
            "window": self.window,
            "request_count": int(self.request_count),
            "total_cost": round(self.total_cost, 6),
            "compression_savings": self.compression_savings.to_json(),
            "cache_savings": self.cache_savings.to_json(),
            "unattributable": self.unattributable.to_json(),
            "db_path": self.db_path,
        }


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------


def _rate_for(model: Optional[str]) -> tuple[float, float]:
    """Return ``(input_rate, cached_rate)`` per 1M tokens for *model*.

    Routes through the dynamic model registry when available; falls back to a
    conservative default so unknown models never crash and never over-claim.
    """
    try:
        from tokenpak.models import get_rates

        rates = get_rates(model)
        return (
            float(rates.get("input", _DEFAULT_INPUT_RATE)),
            float(rates.get("cached", _DEFAULT_CACHED_RATE)),
        )
    except Exception:
        return (_DEFAULT_INPUT_RATE, _DEFAULT_CACHED_RATE)


# ---------------------------------------------------------------------------
# Window helpers
# ---------------------------------------------------------------------------


def _window_clause(days: int, hours: int) -> tuple[str, list[Any], str]:
    """Build the SQL time filter + a human window label.

    Returns ``(where_sql, params, label)``. ``where_sql`` is empty for all-time.
    """
    total_hours = max(0, int(days)) * 24 + max(0, int(hours))
    if total_hours <= 0:
        return ("", [], "all time")
    parts = []
    if days > 0:
        parts.append(f"{int(days)}d")
    if hours > 0:
        parts.append(f"{int(hours)}h")
    label = "last " + " ".join(parts)
    return ("WHERE timestamp >= datetime('now', ?)", [f"-{total_hours} hours"], label)


# ---------------------------------------------------------------------------
# The one helper
# ---------------------------------------------------------------------------


def savings_report(
    db_path: Optional[str] = None,
    days: int = 0,
    hours: int = 0,
) -> UnifiedSavingsReport:
    """Compute the unified, two-plane savings report from the canonical feed.

    Args:
        db_path: Optional explicit monitor.db path. When ``None`` the canonical
            resolver ``tokenpak._paths.monitor_db()`` is used (same DB the proxy
            writer and every other reader resolve).
        days, hours: Time window (combined). ``0/0`` means all-time.

    Returns:
        An :class:`UnifiedSavingsReport`. When the DB is absent or has no rows
        in the window, ``db_state == DB_STATE_NO_DATA`` and all dollar/token
        figures are zero — callers MUST render "not measured yet", not ``$0``.
    """
    where_sql, params, window_label = _window_clause(days, hours)

    # Resolve the canonical feed.
    resolved: Optional[str]
    if db_path:
        resolved = str(db_path)
    else:
        try:
            from tokenpak import _paths

            p = _paths.monitor_db(mode="read")
            resolved = str(p) if p is not None else None
        except Exception:
            resolved = None

    report = UnifiedSavingsReport(window=window_label, db_path=resolved)

    if resolved is None:
        # No feed at all → genuinely not measured.
        report.db_state = DB_STATE_NO_DATA
        return report

    try:
        conn = sqlite3.connect(resolved, timeout=3)
        conn.row_factory = sqlite3.Row
    except Exception:
        report.db_state = DB_STATE_NO_DATA
        return report

    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(requests)").fetchall()}
        has_origin = "cache_origin" in cols

        origin_select = "COALESCE(cache_origin, 'unknown')" if has_origin else "'unknown'"
        rows = conn.execute(
            f"""
            SELECT
                model,
                {origin_select}                         AS origin,
                COUNT(*)                                AS requests,
                COALESCE(SUM(cache_read_tokens), 0)     AS cache_read,
                COALESCE(SUM(compressed_tokens), 0)     AS compressed,
                COALESCE(SUM(estimated_cost), 0.0)      AS cost
            FROM requests
            {where_sql}
            GROUP BY model, origin
            """,
            params,
        ).fetchall()
    except Exception:
        # Legacy / unreadable schema → treat as no usable feed rather than
        # crashing or inventing a number.
        try:
            conn.close()
        except Exception:
            pass
        report.db_state = DB_STATE_NO_DATA
        return report

    try:
        conn.close()
    except Exception:
        pass

    if not rows:
        # DB present but empty in this window → still "not measured yet".
        report.db_state = DB_STATE_NO_DATA
        return report

    compression_usd = 0.0
    compression_tok = 0
    cache_usd = 0.0
    cache_tok = 0
    unattributable_usd = 0.0
    unattributable_tok = 0
    total_cost = 0.0
    total_requests = 0
    saw_attributed_origin = False

    for row in rows:
        origin = (row["origin"] or "unknown").strip().lower()
        cache_read = int(row["cache_read"] or 0)
        compressed = int(row["compressed"] or 0)
        total_cost += float(row["cost"] or 0.0)
        total_requests += int(row["requests"] or 0)

        input_rate, cached_rate = _rate_for(row["model"])
        # Cache value = tokens read from cache * (full input rate - cached rate).
        cache_value = (cache_read / 1_000_000) * (input_rate - cached_rate)
        # Compression value = tokens eliminated entirely, valued at input rate.
        compression_value = (compressed / 1_000_000) * input_rate

        if origin == _ORIGIN_PROXY:
            saw_attributed_origin = True
            # TokenPak-earned compression (the only credit-to-us figure).
            compression_usd += compression_value
            compression_tok += compressed
            # Proxy-placed cache is real cache value; it stays on the cache
            # plane (observability). The compression plane is the canonical
            # "TokenPak earned" figure, so we never inflate compression with it.
            cache_usd += cache_value
            cache_tok += cache_read
        elif origin == _ORIGIN_CLIENT:
            saw_attributed_origin = True
            # Client placed the cache markers → credited to the caller.
            cache_usd += cache_value
            cache_tok += cache_read
            # compressed_tokens on client-origin (byte-preserved passthrough)
            # rows are legacy accounting, NOT TokenPak-caused — do not claim.
        else:
            # Unknown / NULL / sentinel → conservatively credit cache to client,
            # surface separately, never to TokenPak, never dropped.
            unattributable_usd += cache_value
            unattributable_tok += cache_read
            cache_usd += cache_value
            cache_tok += cache_read

    report.compression_savings = SavingsPlane(
        label="compression", usd=compression_usd, tokens=compression_tok,
        credited_to="tokenpak",
    )
    report.cache_savings = SavingsPlane(
        label="cache", usd=cache_usd, tokens=cache_tok, credited_to="client",
    )
    report.unattributable = SavingsPlane(
        label="unattributable", usd=unattributable_usd, tokens=unattributable_tok,
        credited_to="client",
    )
    report.total_cost = total_cost
    report.request_count = total_requests
    report.db_state = (
        DB_STATE_ATTRIBUTED if saw_attributed_origin else DB_STATE_PRESENT_UNATTRIBUTED
    )
    return report


__all__ = [
    "SavingsPlane",
    "UnifiedSavingsReport",
    "savings_report",
    "DB_STATE_NO_DATA",
    "DB_STATE_PRESENT_UNATTRIBUTED",
    "DB_STATE_ATTRIBUTED",
]
