# SPDX-License-Identifier: Apache-2.0
"""Reader-routing regression tests — every CLI monitor.db reader must
resolve through the canonical resolver (``tokenpak.core.paths.get_db_path``
-> ``tokenpak._paths.monitor_db``), never a hand-rolled path.

Covered readers: ``status``, ``budget``, ``optimize``, ``doctor_claude_code``,
and the fleet rollup in ``cli/_impl.py``. All paths live under a
monkeypatched ``HOME``; no real user data is read.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _hermetic_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("TOKENPAK_DB", "TOKENPAK_MONITOR_DB", "TOKENPAK_HOME"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def make_valid_monitor_db(path: Path) -> Path:
    """A DB the resolver accepts: exists, >=100 bytes, has a requests table."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE requests (id INTEGER PRIMARY KEY, ts TEXT, model TEXT)"
    )
    conn.execute("INSERT INTO requests (ts, model) VALUES ('2026-01-01', 'm')")
    conn.commit()
    conn.close()
    return path


def _reader_paths() -> dict[str, str]:
    """Resolve the monitor DB through every rewired CLI reader."""
    from tokenpak.cli import _impl
    from tokenpak.cli.commands import budget, doctor_claude_code, optimize, status

    return {
        "status": status._get_db_path(),
        "budget": budget._monitor_db_path(),
        "optimize": optimize._monitor_db_path(),
        "doctor_claude_code": str(doctor_claude_code._monitor_db_path()),
        "fleet_impl": _impl._resolve_db_path(),
    }


# ---------------------------------------------------------------------------
# All readers agree with the canonical resolver
# ---------------------------------------------------------------------------


def test_all_readers_resolve_canonical_tpk_db(tmp_path):
    canonical = make_valid_monitor_db(tmp_path / ".tpk" / "monitor.db")
    for name, resolved in _reader_paths().items():
        assert resolved == str(canonical), f"{name} bypassed the resolver"


def test_all_readers_follow_legacy_fallback_chain(tmp_path):
    # Only the oldest no-dot layout exists -> the resolver (and therefore
    # every reader) must pick it up.
    legacy = make_valid_monitor_db(tmp_path / "tokenpak" / "monitor.db")
    for name, resolved in _reader_paths().items():
        assert resolved == str(legacy), f"{name} missed the legacy fallback"


def test_all_readers_honor_env_override(tmp_path, monkeypatch):
    override = make_valid_monitor_db(tmp_path / "custom" / "monitor.db")
    monkeypatch.setenv("TOKENPAK_DB", str(override))
    for name, resolved in _reader_paths().items():
        assert resolved == str(override), f"{name} ignored TOKENPAK_DB"


def test_fresh_install_defaults_to_canonical_not_legacy(tmp_path):
    # No DB anywhere: the resolver's fresh-install answer is ~/.tpk/, never
    # the old hand-rolled legacy defaults (~/tokenpak/, ~/.tokenpak/data/).
    expected = str(tmp_path / ".tpk" / "monitor.db")
    for name, resolved in _reader_paths().items():
        assert resolved == expected, f"{name} fell back to a legacy default"


def test_readers_resolve_at_call_time(tmp_path, monkeypatch):
    # Resolution must not be frozen at import time (the old module-level
    # constants were): flipping HOME flips the answer.
    from tokenpak.cli.commands import budget

    first = budget._monitor_db_path()
    other_home = tmp_path / "second-home"
    monkeypatch.setenv("HOME", str(other_home))
    second = budget._monitor_db_path()
    assert first != second
    assert second == str(other_home / ".tpk" / "monitor.db")


def test_explicit_db_path_argument_still_wins(tmp_path):
    from tokenpak.cli import _impl

    assert _impl._resolve_db_path("/x/y/z.db") == "/x/y/z.db"


# ---------------------------------------------------------------------------
# The hand-rolled module-level path constants must stay gone
# ---------------------------------------------------------------------------


def test_no_stale_module_level_path_constants():
    from tokenpak.cli.commands import budget, optimize, status

    assert not hasattr(status, "DB_DEFAULT")
    assert not hasattr(budget, "_MONITOR_DB")
    assert not hasattr(optimize, "_MONITOR_DB")
