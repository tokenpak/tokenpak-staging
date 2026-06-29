"""Focused home-resolver path-boundary regressions for migrated callsites."""

from __future__ import annotations

import importlib
from pathlib import Path


def test_debug_capture_default_uses_tokenpak_home(tmp_path, monkeypatch):
    from tokenpak.debug import capture as cap

    home = tmp_path / "home"
    tpk_home = tmp_path / "tpk-home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("TOKENPAK_HOME", str(tpk_home))
    monkeypatch.setenv("TOKENPAK_DEBUG_CAPTURE", "hash_only")
    monkeypatch.setattr(cap, "_BLOB_DIR", None)
    monkeypatch.setattr(cap, "_KEY_FILE", None)

    dest = cap.capture("std33", {"q": "x"}, {"ok": True})

    assert dest == tpk_home / "debug" / "std33.hash"
    assert not (home / ".tokenpak").exists()


def test_routing_ledger_default_path_resolves_tokenpak_home(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TOKENPAK_HOME", str(tmp_path / "tpk-home"))

    import tokenpak.routing.routing_ledger as routing_ledger

    routing_ledger = importlib.reload(routing_ledger)

    assert routing_ledger.DEFAULT_LEDGER_PATH == str(tmp_path / "tpk-home" / "routing_ledger.db")
    assert not (Path.cwd() / ".tokenpak" / "routing_ledger.db").exists()


def test_proxy_server_pid_path_has_no_legacy_home_literal():
    source = Path("tokenpak/proxy/server.py").read_text(encoding="utf-8")

    assert 'Path.home() / ".tokenpak" / "proxy.pid"' not in source
    assert '_paths.under("proxy.pid")' in source
