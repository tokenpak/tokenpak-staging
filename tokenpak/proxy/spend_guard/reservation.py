# SPDX-License-Identifier: Apache-2.0
"""Concurrent budget reservations — atomic admission for in-flight requests.

The rolling caps (:mod:`rolling_caps`) account for **settled** usage: rows
the proxy wrote to ``monitor.db`` after a provider response completed. The
policy engine (:mod:`policy`) judges one request at a time. Neither sees
**in-flight concurrency**: N simultaneous requests, each individually under
the cap, can jointly blow through it because every admission check reads the
same settled baseline (Standard 29 §15).

This module closes that hole with a transient reservation ledger:

- On admission, a request **reserves** its projected cost/tokens against the
  shared budget scope (per-agent + per-fleet — the SAME caps the rolling-cap
  plane enforces; there is no second budget).
- Admission is atomic: ``BEGIN IMMEDIATE`` serializes concurrent reservers,
  so the K-th admit sees the K-1 reservations before it and the budget can
  never be jointly exceeded.
- A denied reservation inserts nothing — the caller returns a 402 block and
  the provider is never called (provider-not-called-on-reject invariant).
- On settlement (provider response recorded), :meth:`ReservationStore.settle`
  releases the hold and records actual-vs-reserved for reconciliation.
- Abandoned holds (crash, hung stream, missing settle wiring) expire at
  ``expires_at`` and stop counting — degraded-but-safe in the over-blocking
  direction, never under-protecting.

DB layout follows the ``pending.py`` / ``grants.py`` convention: same SQLite
file (``~/.tokenpak/spend_guard.db``, configurable), lazy ``CREATE TABLE IF
NOT EXISTS``, per-call ``sqlite3.connect`` — this is a transient holding
ledger, NOT a parallel budget store; settled truth stays in ``monitor.db``.
"""

from __future__ import annotations

import os
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .rolling_caps import CapBreach, RollingCapsConfig

# Interim pessimistic ceiling for output-token reservation when the caller
# did not set max_tokens. Refine once the Packet-2 capability registry lands
# (per-model max-output instead of a flat ceiling).
INTERIM_MAX_OUTPUT_RESERVATION = 32_000

# Status tokens returned by :meth:`ReservationStore.reserve`.
RESERVED = "reserved"
DENIED = "denied"


@dataclass
class Reservation:
    """An active hold against the shared budget scope."""

    reservation_id: str
    session_id: str
    fleet_id: str
    agent_id: str
    created_at: float
    expires_at: float
    reserved_input_tokens: int
    reserved_output_tokens: int
    reserved_cost_usd: float
    status: str = "active"  # active | settled | released | expired
    actual_cost_usd: Optional[float] = None
    actual_tokens: Optional[int] = None


@dataclass
class ReservationBreach(CapBreach):
    """A denied admission. Extends :class:`rolling_caps.CapBreach` (same
    cap_dimension vocabulary, same consumer contract) with the settled/
    reserved split so operators can see WHY concurrency tripped a cap that
    settled usage alone would not have."""

    settled_used: float = 0.0
    reserved_active: float = 0.0


def pessimistic_output_reservation(
    max_tokens: Optional[int],
    model_max_context_tokens: Optional[int],
    projected_input_tokens: int,
) -> int:
    """Output-token amount to reserve for one request.

    A caller-declared ``max_tokens`` is the reservation — the provider will
    not produce more. Without one, reserve pessimistically:
    ``min(model_context_window - projected_input_tokens, 32000)`` (interim
    default per Proposal-4 Packet 1A; refine once the Packet-2 capability
    registry lands). Unknown context window → the flat interim ceiling.
    """
    if isinstance(max_tokens, int) and max_tokens > 0:
        return max_tokens
    if (
        isinstance(model_max_context_tokens, int)
        and model_max_context_tokens > 0
    ):
        remaining = model_max_context_tokens - max(0, int(projected_input_tokens))
        return max(0, min(remaining, INTERIM_MAX_OUTPUT_RESERVATION))
    return INTERIM_MAX_OUTPUT_RESERVATION


def _db_path(audit_db_path: str) -> Path:
    p = Path(os.path.expanduser(audit_db_path))
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _connect(path: Path) -> sqlite3.Connection:
    # autocommit mode: reserve()/settle() manage their own BEGIN IMMEDIATE /
    # COMMIT explicitly, which the stdlib's implicit-transaction layer would
    # otherwise fight ("cannot start a transaction within a transaction").
    conn = sqlite3.connect(str(path), timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS budget_reservations (
            reservation_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL DEFAULT '',
            fleet_id TEXT NOT NULL DEFAULT '',
            agent_id TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            reserved_input_tokens INTEGER NOT NULL DEFAULT 0,
            reserved_output_tokens INTEGER NOT NULL DEFAULT 0,
            reserved_cost_usd REAL NOT NULL DEFAULT 0.0,
            status TEXT NOT NULL DEFAULT 'active',
            actual_cost_usd REAL,
            actual_tokens INTEGER
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_resv_active "
        "ON budget_reservations(status, expires_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_resv_agent "
        "ON budget_reservations(agent_id, status)"
    )
    conn.commit()


def _row_to_reservation(row: sqlite3.Row) -> Reservation:
    return Reservation(
        reservation_id=row["reservation_id"],
        session_id=row["session_id"],
        fleet_id=row["fleet_id"],
        agent_id=row["agent_id"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        reserved_input_tokens=row["reserved_input_tokens"],
        reserved_output_tokens=row["reserved_output_tokens"],
        reserved_cost_usd=row["reserved_cost_usd"],
        status=row["status"],
        actual_cost_usd=row["actual_cost_usd"],
        actual_tokens=row["actual_tokens"],
    )


class ReservationStore:
    """SQLite-backed reservation ledger.

    Cheap to construct (holds only a path). Each method opens a fresh
    connection to stay thread-safe under the per-request proxy server.
    """

    def __init__(self, audit_db_path: str = "~/.tokenpak/spend_guard.db"):
        self.path = _db_path(audit_db_path)

    # -- admission -----------------------------------------------------------
    def reserve(
        self,
        *,
        session_id: str,
        fleet_id: str,
        agent_id: str,
        projected_input_tokens: int,
        reserved_output_tokens: int,
        projected_cost_usd: float,
        settled_usage: dict,
        caps: RollingCapsConfig,
        ttl_seconds: int = 600,
        force: bool = False,
        now: Optional[float] = None,
    ) -> tuple[str, Optional[Reservation], Optional[ReservationBreach]]:
        """Atomically check the shared budget and insert a hold.

        Returns ``(status, reservation, breach)`` — exactly one of
        ``reservation`` / ``breach`` is set. Status is :data:`RESERVED` or
        :data:`DENIED`. A denied call inserts NOTHING (the caller must not
        forward to the provider).

        ``settled_usage`` is the rolling-window settled baseline in the
        shape :func:`rolling_caps.compute_rolling_usage` returns. The check
        is ``settled + active_reserved + this_request <= cap`` on each
        engaged dimension, in the rolling-cap check order. The baseline may
        be up to one cache-TTL stale (conservative either way — settled rows
        only grow); the reservation sums are exact under ``BEGIN IMMEDIATE``,
        which is what makes concurrent admission sound.

        ``force=True`` records the hold WITHOUT the budget check — used for
        operator-approved sends (TIP bypass / allow), which are never denied
        but must still be visible to other admissions' accounting.
        """
        ts = time.time() if now is None else now
        reservation = Reservation(
            reservation_id="tpr_" + secrets.token_hex(8),
            session_id=session_id or "",
            fleet_id=fleet_id or "",
            agent_id=(agent_id or "").lower(),
            created_at=ts,
            expires_at=ts + max(1, int(ttl_seconds)),
            reserved_input_tokens=max(0, int(projected_input_tokens)),
            reserved_output_tokens=max(0, int(reserved_output_tokens)),
            reserved_cost_usd=max(0.0, float(projected_cost_usd)),
        )
        resv_tokens = (
            reservation.reserved_input_tokens + reservation.reserved_output_tokens
        )

        conn = _connect(self.path)
        try:
            _ensure_schema(conn)
            # IMMEDIATE takes the write lock up front: every concurrent
            # reserve() serializes here, so the sums below can't go stale
            # between read and insert.
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "UPDATE budget_reservations SET status='expired' "
                    "WHERE status='active' AND expires_at <= ?",
                    (ts,),
                )
                if not force:
                    breach = self._check_caps(
                        conn, reservation, resv_tokens, settled_usage, caps, ts
                    )
                    if breach is not None:
                        conn.execute("ROLLBACK")
                        return (DENIED, None, breach)
                conn.execute(
                    """INSERT INTO budget_reservations
                           (reservation_id, session_id, fleet_id, agent_id,
                            created_at, expires_at, reserved_input_tokens,
                            reserved_output_tokens, reserved_cost_usd, status)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')""",
                    (reservation.reservation_id, reservation.session_id,
                     reservation.fleet_id, reservation.agent_id,
                     reservation.created_at, reservation.expires_at,
                     reservation.reserved_input_tokens,
                     reservation.reserved_output_tokens,
                     reservation.reserved_cost_usd),
                )
                conn.execute("COMMIT")
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
        finally:
            conn.close()
        return (RESERVED, reservation, None)

    def _check_caps(
        self,
        conn: sqlite3.Connection,
        resv: Reservation,
        resv_tokens: int,
        settled: dict,
        caps: RollingCapsConfig,
        ts: float,
    ) -> Optional[ReservationBreach]:
        """Evaluate every engaged dimension; first breach wins (matches the
        rolling-cap check order so error messages pinpoint the same way)."""
        fleet_cost, fleet_tokens = self._active_sums(conn, ts, agent_id=None)

        def breach(dimension, used_settled, used_reserved, cap, add):
            return ReservationBreach(
                cap_dimension=dimension,
                agent_id=resv.agent_id or "unknown",
                window_seconds=caps.window_seconds,
                used=float(used_settled) + float(used_reserved),
                cap=float(cap),
                projected_add=float(add),
                retry_after_seconds=60,  # holds drain at settle/expiry, much faster than the rolling window
                settled_used=float(used_settled),
                reserved_active=float(used_reserved),
            )

        if resv.agent_id:
            a_cost, a_tokens = self._active_sums(conn, ts, agent_id=resv.agent_id)
            s_cost = float(settled.get("agent_cost_usd", 0.0))
            s_tok = int(settled.get("agent_tokens_total", 0))
            if (
                caps.per_agent_max_cost_usd > 0
                and s_cost + a_cost + resv.reserved_cost_usd > caps.per_agent_max_cost_usd
            ):
                return breach("per_agent_cost_usd", s_cost, a_cost,
                              caps.per_agent_max_cost_usd, resv.reserved_cost_usd)
            if (
                caps.per_agent_max_tokens_total > 0
                and s_tok + a_tokens + resv_tokens > caps.per_agent_max_tokens_total
            ):
                return breach("per_agent_tokens_total", s_tok, a_tokens,
                              caps.per_agent_max_tokens_total, resv_tokens)

        sf_cost = float(settled.get("fleet_cost_usd", 0.0))
        sf_tok = int(settled.get("fleet_tokens_total", 0))
        if (
            caps.per_fleet_max_cost_usd > 0
            and sf_cost + fleet_cost + resv.reserved_cost_usd > caps.per_fleet_max_cost_usd
        ):
            return breach("per_fleet_cost_usd", sf_cost, fleet_cost,
                          caps.per_fleet_max_cost_usd, resv.reserved_cost_usd)
        if (
            caps.per_fleet_max_tokens_total > 0
            and sf_tok + fleet_tokens + resv_tokens > caps.per_fleet_max_tokens_total
        ):
            return breach("per_fleet_tokens_total", sf_tok, fleet_tokens,
                          caps.per_fleet_max_tokens_total, resv_tokens)
        return None

    @staticmethod
    def _active_sums(
        conn: sqlite3.Connection, ts: float, *, agent_id: Optional[str]
    ) -> tuple[float, int]:
        """(cost, input+output tokens) currently held by active reservations."""
        q = (
            "SELECT COALESCE(SUM(reserved_cost_usd), 0.0), "
            "COALESCE(SUM(reserved_input_tokens), 0) + "
            "COALESCE(SUM(reserved_output_tokens), 0) "
            "FROM budget_reservations "
            "WHERE status='active' AND expires_at > ?"
        )
        args: tuple = (ts,)
        if agent_id is not None:
            q += " AND agent_id = ?"
            args = (ts, agent_id)
        row = conn.execute(q, args).fetchone()
        return float(row[0]), int(row[1])

    # -- settlement / release ------------------------------------------------
    def settle(
        self,
        reservation_id: str,
        *,
        actual_cost_usd: Optional[float] = None,
        actual_tokens: Optional[int] = None,
        now: Optional[float] = None,
    ) -> Optional[Reservation]:
        """Release a hold after the provider call settled.

        Records actual-vs-reserved for reconciliation and frees the FULL
        reserved amount — settled truth lives in ``monitor.db`` from this
        point, so the hold must not double-count next to it. Idempotent:
        only an ``active`` row settles; repeat calls return None.
        """
        conn = _connect(self.path)
        try:
            _ensure_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM budget_reservations "
                    "WHERE reservation_id = ? AND status='active'",
                    (reservation_id,),
                ).fetchone()
                if not row:
                    conn.execute("ROLLBACK")
                    return None
                conn.execute(
                    """UPDATE budget_reservations
                       SET status='settled', actual_cost_usd = ?, actual_tokens = ?
                       WHERE reservation_id = ?""",
                    (
                        None if actual_cost_usd is None else float(actual_cost_usd),
                        None if actual_tokens is None else int(actual_tokens),
                        reservation_id,
                    ),
                )
                conn.execute("COMMIT")
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
        finally:
            conn.close()
        settled = _row_to_reservation(row)
        settled.status = "settled"
        settled.actual_cost_usd = actual_cost_usd
        settled.actual_tokens = actual_tokens
        return settled

    def release(self, reservation_id: str) -> bool:
        """Free a hold without settlement (e.g. provider send failed before
        any usage accrued). Returns True if an active row was released."""
        conn = _connect(self.path)
        try:
            _ensure_schema(conn)
            cur = conn.execute(
                "UPDATE budget_reservations SET status='released' "
                "WHERE reservation_id = ? AND status='active'",
                (reservation_id,),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def expire_old(self, *, now: Optional[float] = None) -> int:
        """Mark all active holds past expires_at as expired. Returns count.

        Also runs inline at every :meth:`reserve` so abandoned holds can
        never wedge admission even if nothing calls this explicitly.
        """
        ts = time.time() if now is None else now
        conn = _connect(self.path)
        try:
            _ensure_schema(conn)
            cur = conn.execute(
                "UPDATE budget_reservations SET status='expired' "
                "WHERE status='active' AND expires_at <= ?",
                (ts,),
            )
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()

    # -- observability ---------------------------------------------------------
    def get_by_id(self, reservation_id: str) -> Optional[Reservation]:
        conn = _connect(self.path)
        try:
            _ensure_schema(conn)
            row = conn.execute(
                "SELECT * FROM budget_reservations WHERE reservation_id = ?",
                (reservation_id,),
            ).fetchone()
        finally:
            conn.close()
        return _row_to_reservation(row) if row else None

    def active_totals(
        self, *, agent_id: Optional[str] = None, now: Optional[float] = None
    ) -> dict:
        """{"cost_usd": float, "tokens": int} currently held (fleet-wide, or
        one agent's). Used by tests and `tokenpak doctor`."""
        ts = time.time() if now is None else now
        conn = _connect(self.path)
        try:
            _ensure_schema(conn)
            cost, tokens = self._active_sums(
                conn, ts, agent_id=(agent_id.lower() if agent_id else None)
            )
        finally:
            conn.close()
        return {"cost_usd": cost, "tokens": tokens}


def settle_reservation(
    audit_db_path: str,
    reservation_id: str,
    *,
    actual_cost_usd: Optional[float] = None,
    actual_tokens: Optional[int] = None,
) -> bool:
    """Convenience for the proxy response path: settle + audit in one call.

    Best-effort on the audit side (never raises into the response path).
    Returns True when an active reservation was settled.
    """
    settled = ReservationStore(audit_db_path).settle(
        reservation_id,
        actual_cost_usd=actual_cost_usd,
        actual_tokens=actual_tokens,
    )
    if settled is None:
        return False
    try:
        from .audit import write_audit
        write_audit(
            audit_db_path,
            event_type="reservation_settled",
            session_id=settled.session_id,
            decision_str="settle",
            projected_cost=settled.reserved_cost_usd,
            extra={
                "reservation_id": settled.reservation_id,
                "reserved_cost_usd": settled.reserved_cost_usd,
                "actual_cost_usd": actual_cost_usd,
                "released_unused_cost_usd": (
                    None if actual_cost_usd is None
                    else round(max(0.0, settled.reserved_cost_usd - actual_cost_usd), 6)
                ),
            },
        )
    except Exception:
        pass
    return True


__all__ = [
    "INTERIM_MAX_OUTPUT_RESERVATION",
    "RESERVED",
    "DENIED",
    "Reservation",
    "ReservationBreach",
    "ReservationStore",
    "pessimistic_output_reservation",
    "settle_reservation",
]
