# SPDX-License-Identifier: Apache-2.0
"""Truth-model + schema tests for the render-agnostic ``StatusSnapshot`` provider.

Covers the architecture-contract invariants (decisions/
2026-06-20-pakline-statusline-architecture-contract.md §3.3, Std 34
§1.2.1/§1.2.4, Std 07 §3):

- three distinct cost fields, never a conflated ``cost``;
- no-data / empty monitor.db render ``unknown`` (None), never ``0.0``;
- ``routing_mode`` is carried explicitly, never inferred from rows;
- no numeric savings claim anywhere on the contract;
- single resolver only (``_paths.monitor_db()``), best-effort reads.
"""

from __future__ import annotations

import sqlite3

import tokenpak._paths as _paths
from tokenpak.status import snapshot as S
from tokenpak.status.snapshot import (
    RoutingMode,
    StatusField,
    StatusSnapshot,
    StatusSource,
    build_status_snapshot,
)


def _make_monitor_db(tmp_path, rows):
    """Create a minimal monitor.db with the queried ``requests`` columns."""
    db = tmp_path / "monitor.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE requests (timestamp TEXT, estimated_cost REAL)")
    conn.executemany(
        "INSERT INTO requests (timestamp, estimated_cost) VALUES (?, ?)", rows
    )
    conn.commit()
    conn.close()
    return db


def _patch_db(monkeypatch, path_or_none):
    monkeypatch.setattr(_paths, "monitor_db", lambda mode="read": path_or_none)


# --- schema shape ----------------------------------------------------------

def test_to_dict_schema_is_minimal_and_stable(monkeypatch):
    _patch_db(monkeypatch, None)
    d = build_status_snapshot(now=1000.0).to_dict()
    assert set(d) == {
        "schema_version",
        "routing_mode",
        "session_cost",
        "local_observed_spend",
        "session_budget",
        "generated_at",
        "stale",
    }
    assert d["schema_version"] == S.STATUS_SNAPSHOT_SCHEMA_VERSION


def test_three_distinct_cost_fields_no_conflated_cost(monkeypatch):
    _patch_db(monkeypatch, None)
    d = build_status_snapshot().to_dict()
    # the three named fields exist and are distinct provenance carriers
    for key in ("session_cost", "local_observed_spend", "session_budget"):
        assert set(d[key]) == {"value", "source"}
    # a single conflated ``cost`` field is forbidden by the truth model
    assert "cost" not in d


# --- honesty: no-data is not zero -----------------------------------------

def test_no_monitor_db_is_unknown_never_zero(monkeypatch):
    _patch_db(monkeypatch, None)
    snap = build_status_snapshot()
    assert snap.local_observed_spend.value is None
    assert snap.local_observed_spend.source is StatusSource.UNKNOWN


def test_empty_monitor_db_is_unknown_never_zero(monkeypatch, tmp_path):
    db = _make_monitor_db(tmp_path, rows=[])  # table present, zero rows
    _patch_db(monkeypatch, db)
    snap = build_status_snapshot()
    # empty ledger ⇒ unknown, NOT 0.0 (Std 34 §1.2.4)
    assert snap.local_observed_spend.value is None
    assert snap.local_observed_spend.source is StatusSource.UNKNOWN


def test_monitor_db_with_rows_sums_estimated_cost(monkeypatch, tmp_path):
    db = _make_monitor_db(
        tmp_path,
        rows=[("2026-06-29T10:00:00", 1.50), ("2026-06-29T11:00:00", 2.25)],
    )
    _patch_db(monkeypatch, db)
    snap = build_status_snapshot()
    assert snap.local_observed_spend.value == 3.75
    assert snap.local_observed_spend.source is StatusSource.MONITOR_DB


def test_db_read_error_is_unknown_best_effort(monkeypatch, tmp_path):
    # Resolver returns a path that is not a usable DB ⇒ read raises ⇒ unknown.
    bogus = tmp_path / "not-a-db.sqlite"
    bogus.write_text("this is not sqlite")
    _patch_db(monkeypatch, bogus)
    snap = build_status_snapshot()
    assert snap.local_observed_spend.value is None
    assert snap.local_observed_spend.source is StatusSource.UNKNOWN


def test_window_filters_rows(monkeypatch, tmp_path):
    db = _make_monitor_db(
        tmp_path,
        rows=[
            ("1999-01-01T00:00:00", 99.0),   # ancient — excluded by a recent window
            ("2999-01-01T00:00:00", 4.0),    # future — inside any "since" window
        ],
    )
    _patch_db(monkeypatch, db)
    snap = build_status_snapshot(window="-1 day")
    assert snap.local_observed_spend.value == 4.0
    assert snap.local_observed_spend.source is StatusSource.MONITOR_DB


# --- routing_mode is explicit, never inferred ------------------------------

def test_routing_mode_carried_explicitly(monkeypatch):
    _patch_db(monkeypatch, None)
    assert build_status_snapshot(routing_mode="native").routing_mode is RoutingMode.NATIVE
    assert build_status_snapshot(routing_mode=RoutingMode.PROXY).routing_mode is RoutingMode.PROXY


def test_routing_mode_never_inferred_from_rows(monkeypatch, tmp_path):
    # Even with real monitor.db rows present, routing_mode must NOT be inferred:
    # an unspecified routing_mode stays UNKNOWN regardless of ledger contents.
    db = _make_monitor_db(tmp_path, rows=[("2026-06-29T10:00:00", 5.0)])
    _patch_db(monkeypatch, db)
    snap = build_status_snapshot()  # no routing_mode supplied
    assert snap.routing_mode is RoutingMode.UNKNOWN
    assert snap.local_observed_spend.value == 5.0  # rows were read…
    # …but presence of rows did not promote routing_mode to "proxy"


def test_invalid_routing_mode_degrades_to_unknown(monkeypatch):
    _patch_db(monkeypatch, None)
    assert build_status_snapshot(routing_mode="bogus").routing_mode is RoutingMode.UNKNOWN
    assert build_status_snapshot(routing_mode=None).routing_mode is RoutingMode.UNKNOWN


# --- session_cost / session_budget evidence passthrough --------------------

def test_session_cost_defaults_unknown_and_passes_through(monkeypatch):
    _patch_db(monkeypatch, None)
    default = build_status_snapshot().session_cost
    assert default.value is None and default.source is StatusSource.UNKNOWN
    given = build_status_snapshot(
        session_cost=0.42, session_cost_source=StatusSource.CLAUDE_STATUSLINE
    ).session_cost
    assert given.value == 0.42 and given.source is StatusSource.CLAUDE_STATUSLINE


def test_session_budget_source_only_when_value_present(monkeypatch):
    _patch_db(monkeypatch, None)
    # absent budget must not claim an advisory origin it does not have
    absent = build_status_snapshot().session_budget
    assert absent.value is None and absent.source is StatusSource.UNKNOWN
    present = build_status_snapshot(session_budget=10.0).session_budget
    assert present.value == 10.0 and present.source is StatusSource.COMPANION_ADVISORY


# --- no savings claim ------------------------------------------------------

def test_no_savings_field_anywhere(monkeypatch, tmp_path):
    db = _make_monitor_db(tmp_path, rows=[("2026-06-29T10:00:00", 7.0)])
    _patch_db(monkeypatch, db)
    blob = repr(build_status_snapshot().to_dict()).lower()
    assert "saved" not in blob
    assert "saving" not in blob


# --- metadata --------------------------------------------------------------

def test_generated_at_and_stale_metadata(monkeypatch):
    _patch_db(monkeypatch, None)
    snap = build_status_snapshot(now=1234.5)
    assert snap.generated_at == 1234.5
    assert snap.stale is False
    # default now (wall clock) is a positive float
    assert build_status_snapshot().generated_at > 0


# --- enums match the contract ---------------------------------------------

def test_source_enum_matches_contract_no_forbidden_names():
    values = {m.value for m in StatusSource}
    assert values == {"monitor_db", "claude_statusline", "companion_advisory", "unknown"}
    # forbidden / dropped sources must not exist
    assert "proxy_ledger" not in values
    assert "legacy_telemetry" not in values
    assert "account_spend" not in values


def test_routing_mode_enum_matches_contract():
    assert {m.value for m in RoutingMode} == {"proxy", "native", "unknown"}


def test_statusfield_known_property():
    assert StatusField(value=1.0).known is True
    assert StatusField().known is False


def test_snapshot_is_frozen_dataclass():
    snap = StatusSnapshot()
    try:
        snap.stale = True  # type: ignore[misc]
    except Exception as exc:  # FrozenInstanceError
        assert "frozen" in type(exc).__name__.lower() or "cannot assign" in str(exc).lower()
    else:
        raise AssertionError("StatusSnapshot must be immutable (frozen)")
