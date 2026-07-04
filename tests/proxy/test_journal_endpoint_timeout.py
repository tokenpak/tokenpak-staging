# SPDX-License-Identifier: Apache-2.0
"""Regression coverage for the journal POST endpoint timeout-under-load fix.

Incident (2026-07-02): after the status/budget MCP timeout mitigation, a
companion ``journal_write`` still timed out once under load with
``{"error": "proxy_unreachable", "detail": "timed out"}``.

Root cause on the proxy side: ``_get_journal_store()`` built a fresh
``JournalStore`` on every POST, so each request re-ran ``_init_db()`` (a
WAL-mode switch + schema DDL, each taking a brief write lock) and opened a
second connection for the write. Under a burst of concurrent journal POSTs —
each served in its own handler thread by the threaded proxy — those
per-request connections contended on the SQLite write lock and busy-waited up
to ``BUSY_TIMEOUT_MS`` (5s), which is exactly the companion MCP client
timeout, so a contended write timed out.

The fix (proxy-side, endpoint-scoped — the MCP caller-side timeout
classification is delivered separately by the companion MCP transport patch):

  * memoize the ``JournalStore`` per db_path so the WAL/schema setup runs once
    and writes serialize cheaply through the store's single lock-guarded
    persistent connection instead of racing on the file lock; and
  * classify SQLite contention (``database is locked`` / ``busy``) as a
    distinct ``journal_contended`` (503) terminal error, so a slow/contended
    journal is distinguishable from a hard write failure.

These tests validate the behavior entirely in-process — no live proxy is
started and no service is restarted.
"""

from __future__ import annotations

import io
import json
import sqlite3
import threading
import time

import pytest

from tokenpak.proxy import app_endpoints as ae

# ---------------------------------------------------------------------------
# Minimal handler harness (mirrors tests/proxy/test_journal_handoff_capture.py)
# ---------------------------------------------------------------------------


class FakeHandler:
    """Minimal stand-in for the HTTP handler `_send_json` writes through."""

    def __init__(self) -> None:
        self.status = None
        self.headers_sent: dict = {}
        self.wfile = io.BytesIO()
        self.headers: dict = {}

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.headers_sent[key] = value

    def end_headers(self):
        pass

    def body(self):
        return json.loads(self.wfile.getvalue().decode("utf-8"))


def _post_journal(session_id: str, content: str, entry_type: str = "user") -> FakeHandler:
    h = FakeHandler()
    ae._handle_journal_post(h, session_id, {"content": content, "entry_type": entry_type})
    return h


@pytest.fixture(autouse=True)
def _reset_store_cache():
    """The store cache is a module global; clear it around every test so
    per-test tmp journal.db isolation holds and no writer conn leaks."""
    ae._reset_journal_store_cache()
    yield
    ae._reset_journal_store_cache()


# ---------------------------------------------------------------------------
# Memoization — the root-cause fix
# ---------------------------------------------------------------------------


def test_journal_store_is_memoized_per_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENPAK_COMPANION_JOURNAL_DIR", str(tmp_path))
    first = ae._get_journal_store()
    second = ae._get_journal_store()
    assert first is second, "journal store must be reused, not rebuilt per call"


def test_journal_store_constructed_once_across_posts(tmp_path, monkeypatch):
    """Direct discriminator of the fix: N journal POSTs must construct the
    JournalStore exactly once (pre-fix this was once per request, which is
    what re-ran the WAL switch + schema DDL and drove the contention)."""
    monkeypatch.setenv("TOKENPAK_COMPANION_JOURNAL_DIR", str(tmp_path))

    import tokenpak.companion.journal.store as jstore

    calls = {"n": 0}
    orig_init = jstore.JournalStore.__init__

    def counting_init(self, *args, **kwargs):
        calls["n"] += 1
        orig_init(self, *args, **kwargs)

    monkeypatch.setattr(jstore.JournalStore, "__init__", counting_init)

    for i in range(6):
        h = _post_journal("sess-once", f"entry {i}")
        assert h.status == 200

    assert calls["n"] == 1, f"expected 1 store construction, got {calls['n']}"

    # Sanity: all six writes landed under the single reused store.
    con = sqlite3.connect(str(tmp_path / "journal.db"))
    n_entries = con.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    con.close()
    assert n_entries == 6


def test_reset_cache_forces_fresh_store(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENPAK_COMPANION_JOURNAL_DIR", str(tmp_path))
    first = ae._get_journal_store()
    ae._reset_journal_store_cache()
    second = ae._get_journal_store()
    assert first is not second


# ---------------------------------------------------------------------------
# Bounded behavior under concurrency — the incident scenario
# ---------------------------------------------------------------------------


def test_journal_posts_concurrent_are_bounded_and_lossless(tmp_path, monkeypatch):
    """A burst of concurrent journal POSTs (each in its own thread, as the
    threaded proxy serves them) must all succeed, lose no writes, and finish
    well inside the 5s busy-timeout window that used to swallow them."""
    monkeypatch.setenv("TOKENPAK_COMPANION_JOURNAL_DIR", str(tmp_path))

    n_threads = 6
    per_thread = 5
    statuses: list[int] = []
    lock = threading.Lock()
    barrier = threading.Barrier(n_threads)

    def worker(tid: int) -> None:
        barrier.wait()  # maximize overlap
        for k in range(per_thread):
            h = _post_journal(f"sess-{tid}", f"t{tid}-e{k}")
            with lock:
                statuses.append(h.status)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    start = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    elapsed = time.monotonic() - start

    assert all(not t.is_alive() for t in threads), "a journal POST thread hung"
    assert len(statuses) == n_threads * per_thread
    assert set(statuses) == {200}, f"non-200 journal POSTs: {sorted(set(statuses))}"
    # Serialized sub-millisecond writes; the generous bound still hard-fails if
    # per-request busy-wait contention regressed.
    assert elapsed < 15, f"concurrent journal POSTs took {elapsed:.1f}s"

    con = sqlite3.connect(str(tmp_path / "journal.db"))
    n_entries = con.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    n_sessions = con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    con.close()
    assert n_entries == n_threads * per_thread
    assert n_sessions == n_threads


# ---------------------------------------------------------------------------
# Terminal error metadata — distinguish contention from a hard failure
# ---------------------------------------------------------------------------


def test_journal_contention_classified_as_503_journal_contended(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENPAK_COMPANION_JOURNAL_DIR", str(tmp_path))
    store = ae._get_journal_store()

    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "add_entry", locked)
    h = _post_journal("sess-locked", "content")
    assert h.status == 503
    assert h.body()["error"] == "journal_contended"


def test_journal_busy_also_classified_as_contended(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENPAK_COMPANION_JOURNAL_DIR", str(tmp_path))
    store = ae._get_journal_store()

    def busy(*args, **kwargs):
        raise sqlite3.OperationalError("database is busy")

    monkeypatch.setattr(store, "add_entry", busy)
    h = _post_journal("sess-busy", "content")
    assert h.status == 503
    assert h.body()["error"] == "journal_contended"


def test_journal_non_contention_operationalerror_stays_500(tmp_path, monkeypatch):
    """A non-lock OperationalError (e.g. schema fault) is a hard failure, not
    contention — it must NOT be mislabeled as retryable contention."""
    monkeypatch.setenv("TOKENPAK_COMPANION_JOURNAL_DIR", str(tmp_path))
    store = ae._get_journal_store()

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("no such table: entries")

    monkeypatch.setattr(store, "add_entry", broken)
    h = _post_journal("sess-broken", "content")
    assert h.status == 500
    assert h.body()["error"] == "journal_write_failed"


def test_journal_generic_exception_stays_500(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENPAK_COMPANION_JOURNAL_DIR", str(tmp_path))
    store = ae._get_journal_store()

    def kaboom(*args, **kwargs):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(store, "add_entry", kaboom)
    h = _post_journal("sess-kaboom", "content")
    assert h.status == 500
    assert h.body()["error"] == "journal_write_failed"
