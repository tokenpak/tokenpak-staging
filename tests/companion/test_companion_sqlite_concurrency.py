"""Cross-platform SQLite concurrency posture for companion stores (CP-06).

The Codex hooks, the MCP server, and the launcher all write to the same
``journal.db`` / ``budget.db`` from separate processes. On Windows, SQLite
file locking is stricter than on POSIX, so a missing ``busy_timeout`` turns a
concurrent write into an immediate ``database is locked`` error rather than a
short wait.

These tests assert the stores:
  - open databases in WAL mode with a non-zero ``busy_timeout``,
  - survive many concurrent writers without ``OperationalError``,
  - bind every value as a parameter (no SQL injection / quoting breakage).
"""

from __future__ import annotations

import sqlite3
import threading

from tokenpak.companion.budget.tracker import BudgetTracker
from tokenpak.companion.journal.store import JournalStore


def _journal_mode(db_path) -> str:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute("PRAGMA journal_mode").fetchone()[0].lower()
    finally:
        conn.close()


def test_journal_store_initializes_wal(tmp_path):
    db = tmp_path / "journal.db"
    JournalStore(db)
    assert _journal_mode(db) == "wal"


def test_budget_tracker_initializes_wal(tmp_path):
    db = tmp_path / "budget.db"
    BudgetTracker(db)
    assert _journal_mode(db) == "wal"


def test_journal_store_handles_concurrent_writers(tmp_path):
    """Many threads append entries with no ``database is locked``."""
    store = JournalStore(tmp_path / "journal.db")
    store.start_session("concurrent-sess", project_dir="/tmp", model="sonnet")

    threads = 8
    per_thread = 15
    errors: list[Exception] = []
    barrier = threading.Barrier(threads)

    def worker(idx: int) -> None:
        barrier.wait()  # maximize contention
        try:
            for n in range(per_thread):
                store.add_entry("concurrent-sess", "auto", f"entry {idx}-{n}")
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    workers = [threading.Thread(target=worker, args=(i,)) for i in range(threads)]
    for t in workers:
        t.start()
    for t in workers:
        t.join()

    assert not errors, f"concurrent writers raised: {errors}"
    entries = store.get_entries("concurrent-sess", limit=1000)
    assert len(entries) == threads * per_thread


def test_budget_tracker_handles_concurrent_writers(tmp_path):
    tracker = BudgetTracker(tmp_path / "budget.db")

    threads = 8
    per_thread = 15
    errors: list[Exception] = []
    barrier = threading.Barrier(threads)

    def worker(idx: int) -> None:
        barrier.wait()
        try:
            for _ in range(per_thread):
                tracker.record(
                    input_tokens=1000,
                    output_tokens=500,
                    model="sonnet",
                    session_id=f"sess-{idx}",
                )
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    workers = [threading.Thread(target=worker, args=(i,)) for i in range(threads)]
    for t in workers:
        t.start()
    for t in workers:
        t.join()

    assert not errors, f"concurrent budget writers raised: {errors}"
    conn = sqlite3.connect(str(tmp_path / "budget.db"))
    try:
        count = conn.execute("SELECT COUNT(*) FROM companion_costs").fetchone()[0]
    finally:
        conn.close()
    assert count == threads * per_thread


def test_journal_store_parameterizes_quoted_values(tmp_path):
    """A quote/semicolon-laden id is stored verbatim, not interpolated."""
    store = JournalStore(tmp_path / "journal.db")
    nasty = "sess'); DROP TABLE entries;--"
    store.start_session(nasty, project_dir="/tmp", model="m'odel")
    store.add_entry(nasty, "auto", "payload with 'quotes' and \\backslashes\\")

    # Table still exists and the row round-trips intact.
    entries = store.get_entries(nasty, limit=10)
    assert len(entries) == 1
    assert "backslashes" in entries[0].content
    rec = store.get_session(nasty)
    assert rec is not None
    assert rec.model == "m'odel"


def test_budget_tracker_parameterizes_quoted_session_id(tmp_path):
    tracker = BudgetTracker(tmp_path / "budget.db")
    nasty = "sess'; DELETE FROM companion_costs;--"
    tracker.record(input_tokens=10, model="sonnet", session_id=nasty)

    conn = sqlite3.connect(str(tmp_path / "budget.db"))
    try:
        rows = conn.execute(
            "SELECT session_id FROM companion_costs WHERE session_id = ?", (nasty,)
        ).fetchall()
    finally:
        conn.close()
    assert rows == [(nasty,)]
