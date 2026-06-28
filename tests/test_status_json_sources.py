"""Status JSON source/freshness contract regressions."""

from __future__ import annotations

import json


def _stub_status_json(monkeypatch):
    from tokenpak.cli.commands import status as status_mod

    monkeypatch.setattr(status_mod, "_fetch", lambda *args, **kwargs: None)
    monkeypatch.setattr(status_mod, "_get_version", lambda: "vtest")
    monkeypatch.setattr(status_mod, "_get_db_path", lambda: "/tmp/tokenpak-monitor.db")
    monkeypatch.setattr(
        status_mod,
        "_calculate_fleet_savings",
        lambda db_path=None, period=None: {"period": period, "totals": {}, "models": []},
    )
    monkeypatch.setattr(
        status_mod,
        "_query_tip_cache_attribution",
        lambda db_path=None, days=0, hours=0: {"source": "test"},
    )
    return status_mod


def test_status_json_history_requires_explicit_all(monkeypatch, capsys):
    status_mod = _stub_status_json(monkeypatch)

    status_mod._run_json()
    default_payload = json.loads(capsys.readouterr().out)
    status_mod._run_json(all_time=True)
    explicit_payload = json.loads(capsys.readouterr().out)

    assert "all_time" not in default_payload["savings"]
    assert "all_time" in explicit_payload["savings"]


def test_status_json_tags_live_proxy_source(monkeypatch, capsys):
    status_mod = _stub_status_json(monkeypatch)

    status_mod._run_json(proxy_base="http://127.0.0.1:59231")
    payload = json.loads(capsys.readouterr().out)

    assert payload["proxy"]["source"] == {
        "kind": "proxy_http",
        "base_url": "http://127.0.0.1:59231",
        "freshness": "unreachable",
    }


def test_status_json_tags_historical_savings_source(monkeypatch, capsys):
    status_mod = _stub_status_json(monkeypatch)

    status_mod._run_json(all_time=True)
    payload = json.loads(capsys.readouterr().out)

    assert payload["savings"]["last_24h"]["source"] == {
        "kind": "monitor_db",
        "path": "/tmp/tokenpak-monitor.db",
        "freshness": "historical",
        "period": "24h",
    }
    assert payload["savings"]["all_time"]["source"]["period"] == "all_time"


def test_status_run_forwards_all_flag_to_json(monkeypatch):
    from tokenpak.cli.commands import status as status_mod

    captured: dict = {}
    monkeypatch.setattr(status_mod, "_run_json", lambda **kwargs: captured.update(kwargs))

    status_mod.run(as_json=True, all_time=True)

    assert captured["all_time"] is True
