"""Spend Guard audit-store connection safety regressions."""

from __future__ import annotations

from tokenpak.proxy.spend_guard import audit


def test_audit_connection_applies_busy_timeout(tmp_path):
    path = audit._db_path(str(tmp_path / "spend_guard.db"))

    conn = audit._connect(path)
    try:
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 5000
    finally:
        conn.close()
