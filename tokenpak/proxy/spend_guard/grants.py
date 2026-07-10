# SPDX-License-Identifier: Apache-2.0
"""Session-scoped Yes-grant store — TTL-bounded approval for a whole turn.

A single ``yes`` (POSITIVE intent) or ``[TIP: allow=session ttl=<sec> max=$<usd>]``
creates a grant so subsequent requests in the same agentic turn skip the
soft-block band without re-prompting the operator. The grant is keyed by the
composite ``(session_id, fleet_id, principal/agent_id)`` (Standard 29 §"Yes-grant
scope", W1) — a leaked or replayed ``session_id`` from a different principal
cannot redeem it.

Redemptions still increment rolling counters + audit normally; the grant only
removes the interactive Yes/No prompt, it is NOT a spend exemption. The
hard-block band and rolling caps remain non-bypassable (W5).

DB layout follows the ``pending.py`` convention: one file at
``~/.tokenpak/spend_guard.db`` (configurable), lazy ``CREATE TABLE IF NOT
EXISTS``, per-call ``sqlite3.connect`` (no pool — the proxy is per-request
threaded). Grants are ephemeral; they are NOT meant to survive a proxy
restart (short TTL — losing them just re-prompts once).

**Fail-closed invariant (W3):** the read/redeem methods deliberately let
SQLite errors propagate so the orchestrator can catch them and fall through
to the normal block band (never auto-allow on a read error).
"""

from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class SessionGrant:
    """An active turn-scoped approval grant."""

    session_id: str
    fleet_id: str
    agent_id: str
    granted_at: float
    expires_at: float
    granted_by_pending_id: str
    grant_kind: str
    # None → no dollar ceiling (TTL is the only bound). A float is a hard
    # ceiling that decrements per redemption (W4).
    max_cost_usd_remaining: Optional[float]


# Status tokens returned by :meth:`GrantStore.redeem`.
REDEEMED = "redeemed"
NO_GRANT = "no_grant"
EXPIRED = "expired"
EXHAUSTED = "exhausted"


def _db_path(audit_db_path: str) -> Path:
    p = Path(os.path.expanduser(audit_db_path))
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS session_grants (
            session_id TEXT NOT NULL,
            fleet_id TEXT NOT NULL DEFAULT '',
            agent_id TEXT NOT NULL DEFAULT '',
            granted_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            granted_by_pending_id TEXT NOT NULL DEFAULT '',
            grant_kind TEXT NOT NULL DEFAULT 'session',
            max_cost_usd_remaining REAL,
            PRIMARY KEY (session_id, fleet_id, agent_id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_grants_expiry ON session_grants(expires_at)"
    )
    conn.commit()


def _row_to_grant(row: sqlite3.Row) -> SessionGrant:
    rem = row["max_cost_usd_remaining"]
    return SessionGrant(
        session_id=row["session_id"],
        fleet_id=row["fleet_id"],
        agent_id=row["agent_id"],
        granted_at=row["granted_at"],
        expires_at=row["expires_at"],
        granted_by_pending_id=row["granted_by_pending_id"],
        grant_kind=row["grant_kind"],
        max_cost_usd_remaining=(None if rem is None else float(rem)),
    )


class GrantStore:
    """SQLite-backed session-grant store.

    Cheap to construct (holds only a path). Each method opens a fresh
    connection to stay thread-safe under the per-request proxy server.
    """

    def __init__(self, audit_db_path: str = "~/.tokenpak/spend_guard.db"):
        self.path = _db_path(audit_db_path)

    # -- write -------------------------------------------------------------
    def create(
        self,
        *,
        session_id: str,
        fleet_id: str,
        agent_id: str,
        ttl_seconds: int,
        granted_by_pending_id: str = "",
        grant_kind: str = "session",
        max_cost_usd: Optional[float] = None,
        now: Optional[float] = None,
    ) -> SessionGrant:
        """Insert (or replace) a grant for the composite key. Returns it.

        Re-approving an active session refreshes the TTL and budget by
        replacing the row (INSERT OR REPLACE on the composite PK).
        """
        ts = time.time() if now is None else now
        expires_at = ts + max(1, int(ttl_seconds))
        rem = None if max_cost_usd is None else float(max_cost_usd)
        conn = _connect(self.path)
        try:
            _ensure_schema(conn)
            conn.execute(
                """INSERT OR REPLACE INTO session_grants
                       (session_id, fleet_id, agent_id, granted_at, expires_at,
                        granted_by_pending_id, grant_kind, max_cost_usd_remaining)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (session_id, fleet_id, agent_id, ts, expires_at,
                 granted_by_pending_id, grant_kind, rem),
            )
            conn.commit()
        finally:
            conn.close()
        return SessionGrant(
            session_id=session_id,
            fleet_id=fleet_id,
            agent_id=agent_id,
            granted_at=ts,
            expires_at=expires_at,
            granted_by_pending_id=granted_by_pending_id,
            grant_kind=grant_kind,
            max_cost_usd_remaining=rem,
        )

    # -- read --------------------------------------------------------------
    def get_active(
        self,
        session_id: str,
        fleet_id: str,
        agent_id: str,
        *,
        now: Optional[float] = None,
    ) -> Optional[SessionGrant]:
        """Return the grant for the exact composite key if still live.

        A grant is live only while ``now < expires_at`` (W7 — strict; a grant
        is dead the instant it reaches ``expires_at``). Cross-key lookups
        (different session/fleet/agent) miss by construction (W6).

        Lets SQLite errors propagate — the caller fails closed (W3).
        """
        ts = time.time() if now is None else now
        conn = _connect(self.path)
        try:
            _ensure_schema(conn)
            row = conn.execute(
                """SELECT * FROM session_grants
                   WHERE session_id = ? AND fleet_id = ? AND agent_id = ?""",
                (session_id, fleet_id, agent_id),
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return None
        grant = _row_to_grant(row)
        if ts >= grant.expires_at:
            return None
        return grant

    def redeem(
        self,
        session_id: str,
        fleet_id: str,
        agent_id: str,
        projected_cost_usd: float,
        *,
        now: Optional[float] = None,
    ) -> tuple[str, Optional[SessionGrant]]:
        """Atomically check + spend a grant for one request.

        Returns ``(status, grant)`` where status is one of
        :data:`REDEEMED`, :data:`NO_GRANT`, :data:`EXPIRED`, :data:`EXHAUSTED`.

        - W7 expiry: ``now >= expires_at`` → row deleted, ``EXPIRED``.
        - W4 budget: when a dollar ceiling is set and this request's projected
          cost cannot be fully covered by the remaining budget, the grant is
          spent out — row deleted, ``EXHAUSTED`` (this request falls back to
          the block band; cumulative redeemed spend never exceeds ``max``).
          Otherwise the budget decrements and, if it hits zero, the row is
          deleted so the NEXT request re-prompts.

        Lets SQLite errors propagate — the caller fails closed (W3).
        """
        ts = time.time() if now is None else now
        conn = _connect(self.path)
        try:
            _ensure_schema(conn)
            row = conn.execute(
                """SELECT * FROM session_grants
                   WHERE session_id = ? AND fleet_id = ? AND agent_id = ?""",
                (session_id, fleet_id, agent_id),
            ).fetchone()
            if not row:
                return (NO_GRANT, None)
            grant = _row_to_grant(row)

            if ts >= grant.expires_at:
                conn.execute(
                    """DELETE FROM session_grants
                       WHERE session_id = ? AND fleet_id = ? AND agent_id = ?""",
                    (session_id, fleet_id, agent_id),
                )
                conn.commit()
                return (EXPIRED, grant)

            rem = grant.max_cost_usd_remaining
            if rem is not None:
                cost = max(0.0, float(projected_cost_usd))
                if cost > rem:
                    # Cannot fully cover → spend out without over-running.
                    conn.execute(
                        """DELETE FROM session_grants
                           WHERE session_id = ? AND fleet_id = ? AND agent_id = ?""",
                        (session_id, fleet_id, agent_id),
                    )
                    conn.commit()
                    return (EXHAUSTED, grant)
                new_rem = rem - cost
                if new_rem <= 0:
                    conn.execute(
                        """DELETE FROM session_grants
                           WHERE session_id = ? AND fleet_id = ? AND agent_id = ?""",
                        (session_id, fleet_id, agent_id),
                    )
                else:
                    conn.execute(
                        """UPDATE session_grants SET max_cost_usd_remaining = ?
                           WHERE session_id = ? AND fleet_id = ? AND agent_id = ?""",
                        (new_rem, session_id, fleet_id, agent_id),
                    )
                conn.commit()
                grant.max_cost_usd_remaining = new_rem
                return (REDEEMED, grant)

            # Unbounded budget — TTL is the only bound.
            return (REDEEMED, grant)
        finally:
            conn.close()

    # -- discard / cleanup -------------------------------------------------
    def discard(self, session_id: str, fleet_id: str, agent_id: str) -> bool:
        """Delete a grant for the composite key. Returns True if one existed."""
        conn = _connect(self.path)
        try:
            _ensure_schema(conn)
            cur = conn.execute(
                """DELETE FROM session_grants
                   WHERE session_id = ? AND fleet_id = ? AND agent_id = ?""",
                (session_id, fleet_id, agent_id),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def expire_old(self, *, now: Optional[float] = None) -> int:
        """Delete all grants past their ``expires_at``. Returns count."""
        ts = time.time() if now is None else now
        conn = _connect(self.path)
        try:
            _ensure_schema(conn)
            cur = conn.execute(
                "DELETE FROM session_grants WHERE expires_at <= ?", (ts,)
            )
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()


__all__ = [
    "SessionGrant",
    "GrantStore",
    "REDEEMED",
    "NO_GRANT",
    "EXPIRED",
    "EXHAUSTED",
]
