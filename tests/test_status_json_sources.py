"""Status JSON source/freshness contract regressions."""

from __future__ import annotations

import json


def _stub_status_json(monkeypatch):
    from tokenpak.cli.commands import status as status_mod

    monkeypatch.setattr(
        status_mod,
        "_fetch",
        lambda url, **_kwargs: (
            {
                "status": "ok",
                "version": "1.7.1",
                "uptime_seconds": 42,
                "compilation_mode": "hybrid",
                "requests_total": 7,
            }
            if url.endswith("/health")
            else {"session": {"session_requests": 7}}
            if url.endswith("/stats")
            else {"cache": "raw"}
        ),
    )
    monkeypatch.setattr(status_mod, "_get_version", lambda: "vtest")
    monkeypatch.setattr(status_mod, "_get_db_path", lambda: "/tmp/tokenpak-monitor.db")
    calls: dict[str, list] = {"savings_periods": [], "tip_windows": []}

    def _savings(db_path=None, period=None):
        calls["savings_periods"].append(period)
        return {"period": period, "totals": {}, "models": []}

    def _tip(db_path=None, days=0, hours=0):
        calls["tip_windows"].append((days, hours))
        return {"source": "test", "window": f"{days}d/{hours}h"}

    monkeypatch.setattr(
        status_mod,
        "_calculate_fleet_savings",
        _savings,
    )
    monkeypatch.setattr(status_mod, "_query_tip_cache_attribution", _tip)
    status_mod._test_calls = calls
    return status_mod


def test_status_json_history_requires_explicit_all(monkeypatch, capsys):
    status_mod = _stub_status_json(monkeypatch)

    status_mod._run_json()
    default_payload = json.loads(capsys.readouterr().out)
    status_mod._run_json(all_time=True)
    explicit_payload = json.loads(capsys.readouterr().out)

    assert "all_time" not in default_payload["savings"]
    assert "all_time" in explicit_payload["savings"]
    assert None not in status_mod._test_calls["savings_periods"][:2]
    assert None in status_mod._test_calls["savings_periods"]


def test_status_json_tags_live_proxy_source(monkeypatch, capsys):
    status_mod = _stub_status_json(monkeypatch)

    status_mod._run_json(proxy_base="http://127.0.0.1:59231")
    payload = json.loads(capsys.readouterr().out)

    assert payload["proxy"]["source"] == {
        "kind": "proxy_http",
        "base_url": "http://127.0.0.1:59231",
        "freshness": "live",
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


def test_status_json_schema_is_slim_and_machine_clean(monkeypatch, capsys):
    status_mod = _stub_status_json(monkeypatch)

    status_mod._run_json()
    payload = json.loads(capsys.readouterr().out)

    assert set(payload) == {
        "schema_version",
        "version",
        "window",
        "proxy",
        "savings",
        "tip_cache",
    }
    assert payload["schema_version"] == status_mod.STATUS_JSON_SCHEMA_VERSION
    assert set(payload["proxy"]) == {
        "reachable",
        "status",
        "version",
        "uptime_seconds",
        "compilation_mode",
        "requests_total",
        "source",
    }
    assert "meme_lines" not in payload
    assert "health" not in payload["proxy"]
    assert "stats" not in payload["proxy"]
    assert "cache" not in payload["proxy"]
    assert payload["proxy"]["requests_total"] == 7


def test_status_json_default_tip_window_is_bounded(monkeypatch, capsys):
    status_mod = _stub_status_json(monkeypatch)

    status_mod._run_json()
    json.loads(capsys.readouterr().out)

    assert status_mod._test_calls["tip_windows"] == [(1, 0)]


def test_status_run_forwards_all_flag_to_json(monkeypatch):
    from tokenpak.cli.commands import status as status_mod

    captured: dict = {}
    monkeypatch.setattr(status_mod, "_run_json", lambda **kwargs: captured.update(kwargs))

    status_mod.run(as_json=True, all_time=True)

    assert captured["all_time"] is True
