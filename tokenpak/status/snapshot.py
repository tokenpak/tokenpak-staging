# SPDX-License-Identifier: Apache-2.0
"""Render-agnostic status provider — the canonical ``StatusSnapshot`` data contract.

This module is the *provider* spine named by the PakLine / universal status-line
architecture contract (``decisions/2026-06-20-pakline-statusline-architecture-contract.md``,
core decision §2 + truth model §3.3). It deliberately ships **two** layers:

1. The **proxy-status probe** (``ProxyStatus`` / ``StatusCache``) — extracted
   verbatim from ``tokenpak.cli.commands.menu_status`` so the interactive menu,
   ``doctor``, and ``_cli_core`` keep their existing honest, non-blocking status
   strip. ``menu_status`` is now a thin re-export shim over this module.

2. The **render-agnostic ``StatusSnapshot`` contract** — a single data object
   that any renderer/adapter (PakLine native / tmux / title / ``--json``) can
   consume. It is *not* tmux, *not* the terminal title, and *not* named PakLine;
   PakLine is only a renderer.

Truth model (architecture contract §3.3 — single canonical resolver, DB-state
honesty "no-data is not zero", and numeric-claim governance):
- Cost is **three distinct fields**, never one conflated ``cost``:
  ``session_cost`` (app-scoped observed), ``local_observed_spend`` (proxy
  observed via ``_paths.monitor_db()``), ``session_budget`` (advisory cap —
  *not* spend). ``account_spend`` is forbidden (it overclaims).
- **Single resolver only** — ``_paths.monitor_db()``. No independent
  ``telemetry.db`` fallback (that re-forks the split-brain). The deprecated
  ``telemetry.db`` *store* is never a status-line source.
- **No-data is not zero** — an absent or empty monitor.db renders ``unknown``
  (``value=None``), never ``0.0``.
- ``routing_mode`` is carried **explicitly** from launcher/config/runtime
  evidence; it is **never inferred from empty rows** (empty rows are
  indistinguishable between no-data / broken-writer / stale-resolver / native).
- **No numeric savings claim** lives on this contract until SAV-RB-01 clears it.
- Every value carries provenance (``source``) and the snapshot carries
  ``generated_at`` + ``stale`` metadata (machine/JSON only; not visible clutter).

Dependencies: stdlib + ``tokenpak._paths`` only (no heavy intra-package imports),
so a future one-shot ``status --line`` adapter can keep its reader import-light
(architecture contract §3.1). ``__all__`` is intentionally empty: this provider
is internal plumbing, not (yet) a frozen public-API surface — consumers import
by explicit path.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from tokenpak import _paths

# Internal plumbing only — keep the public-API snapshot unchanged. The proxy
# probe surface stays publicly recorded via ``menu_status.__all__`` (the shim).
__all__: list[str] = []


# ===========================================================================
# Layer 1 — proxy-status probe (extracted from menu_status; behavior preserved)
# ===========================================================================

# Schema version for the machine-readable (``--json``) proxy snapshot. Bump on
# any field rename/removal so consumers can pin. (status spec F3)
STATUS_SCHEMA_VERSION = 1

_HEALTH_TTL = 1.5      # D2
_STATS_TTL = 7.0       # D2
_BACKOFF = 3.0         # D4
_TIMEOUT = 0.3         # D3 (300ms; hard ceiling < 500ms)


def _port() -> int:
    """Canonical proxy port — config / ``TOKENPAK_PORT``, never hardcoded (D6)."""
    try:
        return int(os.environ.get("TOKENPAK_PORT", "8766"))
    except (TypeError, ValueError):
        return 8766


def _monotonic() -> float:
    # Wrapped so tests can monkeypatch a deterministic clock.
    return time.monotonic()


@dataclass
class ProxyStatus:
    """Honest proxy state. ``None`` means *unknown* — never fabricate."""

    state: str                      # "running" | "stopped" | "starting" | "unknown"
    cost: Optional[float] = None    # today's spend; None when unknown
    saved: Optional[float] = None   # today's savings; None when unknown


class StatusCache:
    """Lazy, TTL'd, backoff-protected proxy-status cache (single-writer)."""

    def __init__(self) -> None:
        self._health: Optional[dict] = None
        self._health_at: float = 0.0
        self._health_state: str = "unknown"
        self._stats: Optional[dict] = None
        self._stats_at: float = 0.0
        self._backoff_until: float = 0.0

    # -- internal probes ---------------------------------------------------
    def _get(self, path: str) -> Optional[dict]:
        url = f"http://127.0.0.1:{_port()}{path}"
        try:
            resp = urllib.request.urlopen(url, timeout=_TIMEOUT)  # noqa: S310 (localhost)
            raw = resp.read()
            if resp.status != 200:
                return None
            return json.loads(raw.decode() or "{}") if raw else {}
        except Exception:  # noqa: BLE001 — re-raised classification happens in caller
            raise

    def _refresh_health(self, now: float) -> None:
        if now < self._backoff_until:
            return  # D4: respect backoff, keep last-good / starting
        if self._health is not None and (now - self._health_at) < _HEALTH_TTL:
            return  # fresh enough (D2)
        try:
            data = self._get("/health")
            self._health = data if data is not None else {}
            self._health_at = now
            self._health_state = "running"
            self._backoff_until = 0.0
        except (TimeoutError, OSError) as exc:
            # Distinguish boot window (timeout) from stopped (refused). (D4)
            self._backoff_until = now + _BACKOFF
            if isinstance(exc, urllib.error.URLError):
                exc = getattr(exc, "reason", exc)
            if isinstance(exc, (TimeoutError,)) or "timed out" in str(exc).lower():
                self._health_state = "starting"
            elif isinstance(exc, ConnectionRefusedError) or "refused" in str(exc).lower():
                self._health_state = "stopped"
            else:
                # Keep last-known state if we had one; else unknown.
                self._health_state = self._health_state if self._health else "unknown"
            self._health = None
        except Exception:  # noqa: BLE001
            self._backoff_until = now + _BACKOFF
            self._health = None
            self._health_state = self._health_state if self._health_state != "unknown" else "unknown"

    def _refresh_stats(self, now: float) -> None:
        if now < self._backoff_until:
            return
        if self._stats is not None and (now - self._stats_at) < _STATS_TTL:
            return
        if self._health_state != "running":
            return  # no point probing stats when the proxy isn't up
        try:
            data = self._get("/stats")
            self._stats = data if data is not None else {}
            self._stats_at = now
        except Exception:  # noqa: BLE001
            # Don't clobber last-good stats on a transient miss; just don't update.
            self._backoff_until = now + _BACKOFF

    # -- public ------------------------------------------------------------
    def snapshot(self, *, probe: bool = True) -> ProxyStatus:
        """Return the current honest status. Never blocks beyond ``_TIMEOUT``.

        ``probe=False`` reads only already-cached state (no network) — used by
        the ``--json`` path so it is instant and deterministic (a fresh process
        with nothing cached reports ``unknown``, never a fabricated value).
        """
        if probe:
            now = _monotonic()
            self._refresh_health(now)
            self._refresh_stats(now)

        cost: Optional[float] = None
        saved: Optional[float] = None
        if self._health_state == "running" and self._stats is not None:
            raw_cost = self._stats.get("cost")
            raw_saved = self._stats.get("cost_saved")
            cost = float(raw_cost) if isinstance(raw_cost, (int, float)) else None
            saved = float(raw_saved) if isinstance(raw_saved, (int, float)) else None
        return ProxyStatus(state=self._health_state, cost=cost, saved=saved)


# Module singleton. D5: only the main render loop calls ``snapshot()``; there is
# no background thread, so this needs no lock.
_CACHE = StatusCache()


def snapshot(*, probe: bool = True) -> ProxyStatus:
    """Process-wide honest status snapshot (cached, non-blocking)."""
    return _CACHE.snapshot(probe=probe)


def json_snapshot() -> dict:
    """Deterministic, schema-versioned status dict for ``tokenpak --json`` (F3).

    Cheap: reads only cached state (no fresh probe is forced), emits stable
    field names, and never fabricates a savings figure (unknown -> null).
    """
    s = snapshot(probe=False)
    return {
        "schema_version": STATUS_SCHEMA_VERSION,
        "proxy": s.state,                 # running|stopped|starting|unknown
        "cost_today": s.cost,             # may be null (honesty — D7)
        "saved_today": s.saved,           # may be null
        "port": _port(),
    }


def reset_cache() -> None:
    """Test hook — clear the module singleton's cached state."""
    global _CACHE
    _CACHE = StatusCache()


# ===========================================================================
# Layer 2 — render-agnostic StatusSnapshot data contract
# ===========================================================================

# Schema version for the render-agnostic ``StatusSnapshot`` (independent of the
# legacy proxy ``STATUS_SCHEMA_VERSION`` above). Bump on field rename/removal.
STATUS_SNAPSHOT_SCHEMA_VERSION = 1

# Read timeout for the monitor.db spend probe — a status cache is best-effort,
# never a ledger, so it must never block a redraw on a busy DB.
_SNAPSHOT_DB_TIMEOUT = 0.3


class StatusSource(str, Enum):
    """Provenance of a status value (architecture contract §3.3 source enum).

    ``proxy_ledger`` is intentionally absent — the proxy writes to monitor.db;
    they are one store. ``legacy_telemetry`` is intentionally absent — the
    deprecated ``telemetry.db`` store is never a status-line source.
    """

    MONITOR_DB = "monitor_db"
    CLAUDE_STATUSLINE = "claude_statusline"
    COMPANION_ADVISORY = "companion_advisory"
    UNKNOWN = "unknown"


class RoutingMode(str, Enum):
    """How the observed traffic reaches the provider.

    Carried explicitly from launcher/config/runtime evidence — **never inferred
    from monitor.db rows** (empty rows cannot distinguish native from a broken
    writer or a stale resolver).
    """

    PROXY = "proxy"
    NATIVE = "native"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class StatusField:
    """A single status value plus its provenance.

    ``value is None`` means *unknown* — the value is never fabricated
    (DB-state honesty: no-data is not zero). ``source`` defaults to ``UNKNOWN``
    so an unset field cannot accidentally claim a real origin.
    """

    value: Optional[float] = None
    source: StatusSource = StatusSource.UNKNOWN

    @property
    def known(self) -> bool:
        return self.value is not None

    def as_dict(self) -> dict[str, Any]:
        return {"value": self.value, "source": self.source.value}


@dataclass(frozen=True)
class StatusSnapshot:
    """Render-agnostic status data contract — the canonical provider object.

    Renderers (PakLine native / tmux / title / ``--json``) consume this; they
    do not re-derive telemetry. Unknown values render as ``—`` / ``Unknown`` /
    last-good at the render boundary — never ``$0.00``.
    """

    routing_mode: RoutingMode = RoutingMode.UNKNOWN
    session_cost: StatusField = field(default_factory=StatusField)
    local_observed_spend: StatusField = field(default_factory=StatusField)
    session_budget: StatusField = field(default_factory=StatusField)
    generated_at: float = 0.0
    stale: bool = False
    schema_version: int = STATUS_SNAPSHOT_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        """Stable machine-readable projection (for ``--json`` / adapters).

        Deliberately carries **no** savings field (SAV-RB-01 not cleared) and
        **no** conflated ``cost`` field — the three cost fields stay distinct.
        """
        return {
            "schema_version": self.schema_version,
            "routing_mode": self.routing_mode.value,
            "session_cost": self.session_cost.as_dict(),
            "local_observed_spend": self.local_observed_spend.as_dict(),
            "session_budget": self.session_budget.as_dict(),
            "generated_at": self.generated_at,
            "stale": self.stale,
        }


def _coerce_routing_mode(value: "RoutingMode | str | None") -> RoutingMode:
    """Validate explicit routing-mode evidence; unknown/invalid -> UNKNOWN.

    Never consults monitor.db — routing mode is launcher/config/runtime
    evidence, not a function of observed rows.
    """
    if isinstance(value, RoutingMode):
        return value
    if value is None:
        return RoutingMode.UNKNOWN
    try:
        return RoutingMode(str(value))
    except ValueError:
        return RoutingMode.UNKNOWN


def _read_local_observed_spend(
    window: Optional[str] = None,
) -> tuple[Optional[float], StatusSource]:
    """Sum proxy-observed spend from the single canonical monitor.db resolver.

    Returns ``(value, source)``. Honesty contract:
    - no valid monitor.db                  -> ``(None, UNKNOWN)`` (no-data ≠ 0)
    - monitor.db present but **zero rows**  -> ``(None, UNKNOWN)`` (empty ≠ 0)
    - monitor.db with rows                  -> ``(sum(estimated_cost), MONITOR_DB)``
    - any read error                        -> ``(None, UNKNOWN)`` (best-effort)

    ``window`` is an optional SQLite ``datetime('now', ?)`` modifier (e.g.
    ``"-1 day"``); ``None`` sums all observed rows. The caller (a renderer/
    adapter) owns the window choice — the provider does not assume one.
    """
    db = _paths.monitor_db("read")
    if db is None:
        return None, StatusSource.UNKNOWN  # no DB ⇒ unknown, never zero
    try:
        # Read-only URI so a status probe never creates or mutates the ledger.
        conn = sqlite3.connect(
            f"file:{db}?mode=ro", uri=True, timeout=_SNAPSHOT_DB_TIMEOUT
        )
        try:
            if window:
                cur = conn.execute(
                    "SELECT COALESCE(SUM(estimated_cost), 0), COUNT(*) "
                    "FROM requests WHERE timestamp >= datetime('now', ?)",
                    (window,),
                )
            else:
                cur = conn.execute(
                    "SELECT COALESCE(SUM(estimated_cost), 0), COUNT(*) FROM requests"
                )
            total, rows = cur.fetchone()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — best-effort; a status cache is not a ledger
        return None, StatusSource.UNKNOWN
    if not rows:
        return None, StatusSource.UNKNOWN  # empty monitor.db ⇒ unknown, never zero
    return float(total), StatusSource.MONITOR_DB


def build_status_snapshot(
    *,
    routing_mode: "RoutingMode | str | None" = RoutingMode.UNKNOWN,
    session_cost: Optional[float] = None,
    session_cost_source: StatusSource = StatusSource.UNKNOWN,
    session_budget: Optional[float] = None,
    session_budget_source: StatusSource = StatusSource.COMPANION_ADVISORY,
    window: Optional[str] = None,
    now: Optional[float] = None,
) -> StatusSnapshot:
    """Assemble a ``StatusSnapshot`` from explicit evidence + monitor.db truth.

    Only ``local_observed_spend`` is derived from monitor.db here; ``session_cost``
    and ``session_budget`` come from the caller's evidence (Claude stdin /
    companion advisory) because they are not derivable from the proxy ledger
    without a verified session-correlation key (architecture contract §6 gate 3).
    When their evidence is absent they stay ``unknown`` — never fabricated.
    """
    spend_value, spend_source = _read_local_observed_spend(window)

    # session_budget source is only meaningful when a value is present; an
    # absent budget must not claim an advisory origin it does not have.
    budget_source = (
        session_budget_source if session_budget is not None else StatusSource.UNKNOWN
    )

    return StatusSnapshot(
        routing_mode=_coerce_routing_mode(routing_mode),
        session_cost=StatusField(
            value=session_cost,
            source=session_cost_source if session_cost is not None else StatusSource.UNKNOWN,
        ),
        local_observed_spend=StatusField(value=spend_value, source=spend_source),
        session_budget=StatusField(value=session_budget, source=budget_source),
        generated_at=time.time() if now is None else now,
        stale=False,
    )
