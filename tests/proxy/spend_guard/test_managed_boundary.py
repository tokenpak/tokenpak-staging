# SPDX-License-Identifier: Apache-2.0
"""Managed-traffic boundary — Standard 29 §13 contract tests.

Covers the §13.8 required surface for the proxy/spend-guard half of the managed
boundary (L7a):

  * §13.2 — classifier precedence + stable reason literals (all five branches).
  * §13.3 — internal managed markers are stripped before upstream forwarding
            (the §13.8 negative test).
  * §13.4 — idempotent ``request_class`` migrations on both storage surfaces
            (``requests`` and ``spend_guard_audit``), pre-§13 rows default to
            ``external_untagged``.
  * §13.5 — cap-denominator filter: managed-without-agent counts toward the
            fleet aggregate but not a per-agent denominator; raw/external rows
            never inflate a managed denominator; managed-unattributed surfaced.
  * §13.6 — 402 copy cites the canonical class literal and the excluded-spend
            bucket when non-zero.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3

import pytest

from tokenpak.proxy.spend_guard import classifier as c
from tokenpak.proxy.spend_guard import rolling_caps as rc
from tokenpak.proxy.spend_guard.block_response import (
    ERR_ROLLING_CAP_BLOCKED,
    build_rolling_cap_block,
)
from tokenpak.proxy.spend_guard.rolling_caps import CapBreach

# ───────────────────────────── §13.2 classifier ────────────────────────────


def test_classify_header_agent_is_managed():
    r = c.classify({"X-Tokenpak-Agent": "Trix"})
    assert (r.request_class, r.reason) == (c.MANAGED, c.HEADER_AGENT)
    assert r.agent_attribution == "trix"  # lower-cased attribution


def test_classify_header_managed_is_managed_unattributed():
    r = c.classify({"X-Tokenpak-Managed": "1"})
    assert (r.request_class, r.reason) == (c.MANAGED, c.HEADER_MANAGED)
    assert r.agent_attribution == ""


def test_classify_env_launcher_is_managed():
    r = c.classify({"X-Tokenpak-Managed-Env": "1"})
    assert (r.request_class, r.reason) == (c.MANAGED, c.ENV_LAUNCHER)


def test_classify_ua_claude_code_is_raw_observed():
    r = c.classify({"User-Agent": "claude-cli/1.2.3 (external, cli)"})
    assert (r.request_class, r.reason) == (c.RAW_CLAUDE_OBSERVED, c.UA_CLAUDE_CODE)


def test_classify_no_marker_is_external():
    r = c.classify({"User-Agent": "curl/8.4.0"})
    assert (r.request_class, r.reason) == (c.EXTERNAL_UNTAGGED, c.NO_MARKER)


def test_classify_empty_headers_is_external():
    assert c.classify({}).reason == c.NO_MARKER
    assert c.classify(None).reason == c.NO_MARKER


def test_precedence_agent_beats_managed_and_ua():
    # All three markers present → highest precedence (header_agent) wins.
    r = c.classify(
        {
            "X-Tokenpak-Agent": "Sue",
            "X-Tokenpak-Managed": "1",
            "User-Agent": "claude-cli/9",
        }
    )
    assert r.reason == c.HEADER_AGENT


def test_precedence_managed_beats_ua():
    r = c.classify({"X-Tokenpak-Managed": "1", "User-Agent": "claude-cli/9"})
    assert r.reason == c.HEADER_MANAGED


def test_managed_marker_off_value_is_not_managed():
    # An explicit falsey marker must NOT mark the request managed.
    assert c.classify({"X-Tokenpak-Managed": "0"}).request_class == c.EXTERNAL_UNTAGGED
    assert c.classify({"X-Tokenpak-Managed": ""}).request_class == c.EXTERNAL_UNTAGGED


def test_case_insensitive_header_match():
    r = c.classify({"x-tokenpak-agent": "Cali"})
    assert r.request_class == c.MANAGED and r.agent_attribution == "cali"


def test_reason_and_class_literals_are_canonical():
    # No synonyms / plurals — the literal sets are the single source of truth.
    assert c.REQUEST_CLASSES == {"managed", "raw_claude_observed", "external_untagged"}
    assert c.DETECTION_REASONS == {
        "header_agent",
        "header_managed",
        "env_launcher",
        "ua_claude_code",
        "no_marker",
    }


def test_classify_does_not_mutate_headers():
    headers = {"X-Tokenpak-Managed": "1", "User-Agent": "claude-cli/1"}
    before = dict(headers)
    c.classify(headers)
    assert headers == before  # read-only on the request (§13.7)


# ─────────────────── §13.3 / §13.8 internal header strip ────────────────────


def test_strip_managed_headers_removes_internal_markers():
    fwd = {
        "X-Tokenpak-Managed": "1",
        "X-Tokenpak-Agent": "Trix",
        "X-Tokenpak-Managed-Env": "1",
        "Authorization": "Bearer x",
        "Content-Type": "application/json",
    }
    removed = c.strip_managed_headers(fwd)
    # §13.8 negative test — none of the managed markers survive to upstream.
    assert "X-Tokenpak-Managed" not in fwd
    assert "X-Tokenpak-Agent" not in fwd
    assert "X-Tokenpak-Managed-Env" not in fwd
    # Non-internal headers are untouched.
    assert fwd == {"Authorization": "Bearer x", "Content-Type": "application/json"}
    assert set(removed) == {
        "X-Tokenpak-Managed",
        "X-Tokenpak-Agent",
        "X-Tokenpak-Managed-Env",
    }


def test_strip_managed_headers_case_insensitive():
    fwd = {"x-tokenpak-managed": "1", "host": "api"}
    c.strip_managed_headers(fwd)
    assert fwd == {"host": "api"}


def test_strip_managed_headers_noop_when_absent():
    fwd = {"Authorization": "Bearer y"}
    assert c.strip_managed_headers(fwd) == []
    assert fwd == {"Authorization": "Bearer y"}


# ───────────────────────── §13.4 idempotent migrations ──────────────────────


def test_monitor_migration_adds_request_class_idempotent(tmp_path):
    from tokenpak.proxy.monitor import Monitor

    db = tmp_path / "monitor.db"
    # Pre-§13 DB: a minimal requests table + one legacy row, no request_class.
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE requests (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "timestamp TEXT NOT NULL, model TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO requests (timestamp, model) VALUES (?, ?)",
        (dt.datetime.now().isoformat(), "claude-opus-4-8"),
    )
    conn.commit()
    conn.close()

    # First migration via the real Monitor init.
    Monitor(str(db))
    cols = _columns(str(db), "requests")
    assert "request_class" in cols and "request_class_reason" in cols
    # Pre-§13 row surfaces with the default class (§13.4).
    row = sqlite3.connect(str(db)).execute(
        "SELECT request_class, request_class_reason FROM requests"
    ).fetchone()
    assert row == ("external_untagged", "no_marker")

    # Re-running the migration is a no-op (PRAGMA table_info guard) — no raise.
    Monitor(str(db))
    assert "request_class" in _columns(str(db), "requests")


def test_audit_migration_adds_request_class_idempotent(tmp_path):
    from tokenpak.proxy.spend_guard import audit

    db = tmp_path / "spend_guard.db"
    # Pre-§13 audit table (the legacy 11-column schema, no request_class).
    conn = sqlite3.connect(str(db))
    conn.execute(
        """CREATE TABLE spend_guard_audit (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               ts REAL NOT NULL, session_id TEXT NOT NULL DEFAULT '',
               event_type TEXT NOT NULL, decision TEXT NOT NULL DEFAULT '',
               reason TEXT NOT NULL DEFAULT '', projected_tokens INTEGER NOT NULL DEFAULT 0,
               projected_cost_usd REAL NOT NULL DEFAULT 0.0,
               pending_id TEXT NOT NULL DEFAULT '', request_hash TEXT NOT NULL DEFAULT '',
               tip_directive_json TEXT NOT NULL DEFAULT '', extra_json TEXT NOT NULL DEFAULT '')"""
    )
    conn.commit()
    conn.close()
    audit._SCHEMA_READY.clear()

    # First write migrates the table and persists the class + reason.
    audit.write_audit(
        str(db),
        event_type="block",
        session_id="s1",
        request_class="managed",
        request_class_reason="header_agent",
    )
    cols = _columns(str(db), "spend_guard_audit")
    assert "request_class" in cols and "request_class_reason" in cols
    rows = audit.query_recent(str(db))
    assert rows[0]["request_class"] == "managed"
    assert rows[0]["request_class_reason"] == "header_agent"

    # Force the schema check to re-run and prove the PRAGMA guard is idempotent.
    audit._SCHEMA_READY.clear()
    audit.write_audit(str(db), event_type="warn", session_id="s2")
    rows = audit.query_recent(str(db))
    # A row written without an explicit class defaults to the §13.4 sentinels.
    s2 = [r for r in rows if r["session_id"] == "s2"][0]
    assert s2["request_class"] == "external_untagged"
    assert s2["request_class_reason"] == "no_marker"


# ───────────────────────── §13.5 cap-denominator split ──────────────────────


@pytest.fixture
def managed_db(tmp_path):
    """A monitor.db whose ``requests`` table carries ``request_class``."""
    db = tmp_path / "monitor.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        """CREATE TABLE requests (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               timestamp TEXT NOT NULL, model TEXT NOT NULL, request_type TEXT,
               input_tokens INTEGER, output_tokens INTEGER, estimated_cost REAL,
               latency_ms INTEGER, status_code INTEGER, endpoint TEXT,
               cache_read_tokens INTEGER DEFAULT 0, session_id TEXT DEFAULT '',
               request_class TEXT NOT NULL DEFAULT 'external_untagged')"""
    )
    conn.commit()
    conn.close()
    rc.reset_caches_for_testing()
    yield str(db)
    rc.reset_caches_for_testing()


def _insert(db, *, session, cost, request_class, input_tokens=1000, output_tokens=100):
    conn = sqlite3.connect(db)
    conn.execute(
        """INSERT INTO requests
               (timestamp, model, request_type, input_tokens, output_tokens,
                estimated_cost, cache_read_tokens, session_id, request_class)
           VALUES (?, 'claude-opus-4-8', 'chat', ?, ?, ?, 0, ?, ?)""",
        (dt.datetime.now().isoformat(), input_tokens, output_tokens, cost, session, request_class),
    )
    conn.commit()
    conn.close()


def test_managed_without_agent_counts_fleet_not_per_agent(managed_db):
    # One managed row attributed to agent-a; one managed row with no mapping.
    _insert(managed_db, session="s-mapped", cost=5.0, request_class="managed")
    _insert(managed_db, session="s-unmapped", cost=3.0, request_class="managed")
    rc.record_session_agent("s-mapped", "agent-a")

    usage = rc.compute_rolling_usage("agent-a", 3600, monitor_db_path=managed_db)
    # Fleet aggregate includes managed-without-agent (RD-3); per-agent excludes it.
    assert usage["fleet_cost_usd"] == pytest.approx(8.0)
    assert usage["agent_cost_usd"] == pytest.approx(5.0)
    # The unattributed managed spend is surfaced as its own sub-bucket.
    assert usage["managed_unattributed_cost_usd"] == pytest.approx(3.0)


def test_raw_and_external_excluded_from_managed_denominators(managed_db):
    _insert(managed_db, session="s-a", cost=4.0, request_class="managed")
    _insert(managed_db, session="s-a", cost=10.0, request_class="raw_claude_observed")
    _insert(managed_db, session="s-b", cost=7.0, request_class="external_untagged")
    rc.record_session_agent("s-a", "agent-a")

    usage = rc.compute_rolling_usage("agent-a", 3600, monitor_db_path=managed_db)
    # Only the managed row counts toward managed denominators...
    assert usage["fleet_cost_usd"] == pytest.approx(4.0)
    assert usage["agent_cost_usd"] == pytest.approx(4.0)
    # ...and the 17.0 of raw + external spend is surfaced as excluded.
    assert usage["excluded_observed_spend_usd"] == pytest.approx(17.0)


def test_check_rolling_caps_breach_carries_excluded_spend(managed_db):
    # Managed spend below cap, but a large external spend should be excluded
    # AND surfaced; force a per-fleet breach with the managed total.
    _insert(managed_db, session="s-a", cost=50.0, request_class="managed")
    _insert(managed_db, session="s-x", cost=99.0, request_class="external_untagged")
    rc.record_session_agent("s-a", "agent-a")
    cfg = rc.RollingCapsConfig(
        enabled=True,
        per_agent_max_cost_usd=0.0,  # disable per-agent so the fleet cap trips
        per_agent_max_tokens_total=0,
        per_agent_max_cache_read_tokens=0,
        per_fleet_max_cost_usd=40.0,
        per_fleet_max_tokens_total=0,
        per_fleet_max_cache_read_tokens=0,
    )
    breach = rc.check_rolling_caps(
        agent_id="agent-a",
        projected_cost_usd=1.0,
        projected_input_tokens=10,
        projected_output_tokens=10,
        projected_cache_read_tokens=0,
        config=cfg,
        monitor_db_path=managed_db,
    )
    assert breach is not None
    assert breach.cap_dimension == "per_fleet_cost_usd"
    # The fleet denominator is managed-only (50.0), not 149.0.
    assert breach.used == pytest.approx(50.0)
    # The 99.0 external spend the cap ignored is surfaced on the breach.
    assert breach.excluded_observed_spend == pytest.approx(99.0)


def test_legacy_db_without_column_counts_all_rows(tmp_path):
    # Pre-§13 DB (no request_class) → conservative count-all fallback so the
    # cap stays enforced rather than fail-open to zero.
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        """CREATE TABLE requests (
               id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
               model TEXT NOT NULL, input_tokens INTEGER, output_tokens INTEGER,
               estimated_cost REAL, cache_read_tokens INTEGER DEFAULT 0,
               session_id TEXT DEFAULT '')"""
    )
    conn.execute(
        "INSERT INTO requests (timestamp, model, input_tokens, output_tokens, "
        "estimated_cost, session_id) VALUES (?, 'm', 1, 1, 12.0, 's')",
        (dt.datetime.now().isoformat(),),
    )
    conn.commit()
    conn.close()
    rc.reset_caches_for_testing()
    usage = rc.compute_rolling_usage("agent-a", 3600, monitor_db_path=str(db))
    assert usage["fleet_cost_usd"] == pytest.approx(12.0)  # all rows counted
    assert usage["excluded_observed_spend_usd"] == pytest.approx(0.0)
    rc.reset_caches_for_testing()


# ──────────────────────────── §13.6 402 block copy ──────────────────────────


def _decode(breach, **kw) -> dict:
    return json.loads(build_rolling_cap_block(breach, **kw).decode("utf-8"))["error"]


def _fleet_breach(**extra) -> CapBreach:
    return CapBreach(
        cap_dimension="per_fleet_cost_usd",
        agent_id="worker-a",
        window_seconds=3600,
        used=60.0,
        cap=60.0,
        projected_add=0.5,
        retry_after_seconds=1800,
        **extra,
    )


def test_402_cites_external_class_and_exclusion():
    err = _decode(
        _fleet_breach(excluded_observed_spend=99.0),
        request_class="external_untagged",
    )
    assert err["type"] == ERR_ROLLING_CAP_BLOCKED
    # §13.6 — canonical class literal cited verbatim.
    assert err["request_class"] == "external_untagged"
    # §13.5 — excluded spend surfaced.
    assert err["excluded_observed_spend"] == pytest.approx(99.0)
    # §13.6 — exclusion clarified in the human copy for non-managed classes.
    assert "external_untagged" in err["message"]
    assert "NOT counted against the managed-agent cap denominator" in err["message"]


def test_402_managed_class_has_no_exclusion_note():
    err = _decode(_fleet_breach(), request_class="managed")
    assert err["request_class"] == "managed"
    assert "NOT counted against the managed-agent cap denominator" not in err["message"]


def test_402_surfaces_managed_unattributed_when_nonzero():
    err = _decode(
        _fleet_breach(managed_unattributed_spend=7.5), request_class="managed"
    )
    assert err["managed_unattributed_spend"] == pytest.approx(7.5)


def test_402_omits_excluded_spend_when_zero():
    err = _decode(_fleet_breach(), request_class="managed")
    assert "excluded_observed_spend" not in err
    assert "managed_unattributed_spend" not in err


def test_402_backward_compatible_without_request_class():
    # No request_class passed (legacy caller) → no new keys, legacy body intact.
    err = _decode(_fleet_breach())
    assert "request_class" not in err
    assert err["scope"] == "fleet"
    assert err["triggered_by"] == "worker-a"


def _columns(db_path: str, table: str) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()
