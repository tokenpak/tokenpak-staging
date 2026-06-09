# SPDX-License-Identifier: Apache-2.0
"""Caller-side ``skip_reason`` threading: proxy → monitor.db → ``--explain``.

The persistence substrate (the additive ``skip_reason`` column +
``Monitor.log(skip_reason=...)`` param) is covered by
``test_explain_skip_reason.py``. This suite covers the *caller* side: the proxy
now derives an honest per-request skip reason from state already on the request
path and threads it into ``Monitor.log()``, so ``tokenpak status --explain``
renders a real drop reason instead of ``unknown``.

Two layers:

* The pure ``_derive_skip_reason`` helper returns the expected reason for each
  skip branch, and the empty string (non-skip default) when the request WAS
  optimized.
* End-to-end: a derived reason logged via ``Monitor.log()`` is rendered by
  ``status --explain``; an optimized request renders the non-skip value (the
  honest ``unknown`` fallback, since no reason was recorded).
"""
import sqlite3

import pytest

from tokenpak.cli.commands import explain
from tokenpak.proxy.monitor import Monitor
from tokenpak.proxy.server import _derive_skip_reason

# ── Pure derivation: one assertion per branch ─────────────────────────────────

def test_explicit_reason_wins():
    # A concrete upstream reason (e.g. compression forwarded unchanged) takes
    # precedence over every derived branch.
    assert (
        _derive_skip_reason(
            explicit_reason="compression-failed",
            is_byte_preserved=True,
            compilation_mode="transparent",
            compressed_tokens=0,
        )
        == "compression-failed"
    )


def test_byte_preserved_skip():
    assert (
        _derive_skip_reason(is_byte_preserved=True, compressed_tokens=0)
        == "byte-preserved"
    )


def test_transparent_mode_skip():
    assert (
        _derive_skip_reason(compilation_mode="transparent", compressed_tokens=0)
        == "transparent-mode"
    )


def test_no_compression_applied():
    assert (
        _derive_skip_reason(compilation_mode="compress", compressed_tokens=0)
        == "no-compression-applied"
    )


def test_optimized_request_has_no_skip_reason():
    # Compression reduced tokens → request WAS optimized → non-skip default "".
    assert (
        _derive_skip_reason(compilation_mode="compress", compressed_tokens=120)
        == ""
    )


def test_helper_never_raises_on_defaults():
    # Fail-open posture: a bare call yields the non-skip default.
    assert _derive_skip_reason() == "no-compression-applied"


# ── End-to-end: derived reason persists and renders via --explain ─────────────

@pytest.fixture(scope="module")
def built_db(tmp_path_factory):
    """Build the monitor.db schema once via the real ``Monitor`` init + writer."""
    db = tmp_path_factory.mktemp("monitor") / "monitor.db"
    mon = Monitor(str(db))

    # A skipped request: the proxy would derive "byte-preserved" and thread it.
    reason = _derive_skip_reason(is_byte_preserved=True, compressed_tokens=0)
    mon.log(
        model="claude-opus-4-8",
        input_tokens=100,
        output_tokens=10,
        cost=0.01,
        latency_ms=42,
        status_code=200,
        endpoint="/v1/messages",
        skip_reason=reason,
    )
    # An optimized request: non-skip default "" is threaded through.
    opt_reason = _derive_skip_reason(compilation_mode="compress", compressed_tokens=80)
    mon.log(
        model="claude-opus-4-8",
        input_tokens=100,
        output_tokens=10,
        cost=0.01,
        latency_ms=42,
        status_code=200,
        endpoint="/v1/messages",
        skip_reason=opt_reason,
    )

    # Drain the async writer so the rows are visible to the read connection.
    _flush(mon)

    conn = sqlite3.connect(str(db))
    rows = conn.execute(
        "SELECT id, skip_reason FROM requests ORDER BY id"
    ).fetchall()
    conn.close()
    assert len(rows) == 2, f"expected 2 rows, got {rows}"
    return {
        "path": db,
        "rid_skipped": rows[0][0],
        "skipped_value": rows[0][1],
        "rid_optimized": rows[1][0],
        "optimized_value": rows[1][1],
    }


def _flush(mon, timeout_s: float = 5.0):
    """Best-effort drain of the async monitor write queue."""
    import time

    from tokenpak.proxy import monitor as _mon_mod

    q = getattr(_mon_mod, "_DB_WRITE_QUEUE", None)
    deadline = time.time() + timeout_s
    if q is not None:
        try:
            q.join()
        except Exception:
            while not q.empty() and time.time() < deadline:
                time.sleep(0.02)
    # Small settle window for the writer thread to commit.
    time.sleep(0.1)


def test_skipped_request_records_real_reason(built_db):
    assert built_db["skipped_value"] == "byte-preserved"


def test_optimized_request_records_non_skip_value(built_db):
    # The non-skip default is the empty string (never NULL/"unknown").
    assert built_db["optimized_value"] == ""


def test_explain_renders_real_reason_for_skipped(built_db, monkeypatch, capsys):
    monkeypatch.setattr(
        "tokenpak._paths.monitor_db", lambda mode="read": built_db["path"]
    )
    rc = explain.explain_request(built_db["rid_skipped"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "byte-preserved" in out
    assert "unknown (per-request" not in out  # not the honest-unknown fallback


def test_explain_renders_unknown_for_optimized(built_db, monkeypatch, capsys):
    # An optimized request carries no skip reason, so --explain honestly renders
    # the unknown fallback rather than inventing one.
    monkeypatch.setattr(
        "tokenpak._paths.monitor_db", lambda mode="read": built_db["path"]
    )
    rc = explain.explain_request(built_db["rid_optimized"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "unknown" in out.lower()
