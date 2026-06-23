"""Status display savings percentages are derived from token counters."""

from __future__ import annotations

from tokenpak.cli.commands import status


def _fake_fetch(session: dict, health: dict | None = None):
    health = health or {
        "is_degraded": False,
        "uptime_seconds": 60,
        "compression_ratio_avg": 0.0,
    }

    def fetch(url: str, timeout: int = 5):
        if url.endswith("/health"):
            return health
        if url.endswith("/stats/session"):
            return session
        if url.endswith("/degradation"):
            return {"recent_events": [], "status": "ok"}
        return None

    return fetch


def test_full_status_computes_savings_pct_from_actual_tokens(monkeypatch, capsys):
    session = {
        "session_requests": 1,
        "tokens_raw": 1_000,
        "tokens_saved": 250,
        "cache_read_tokens": 0,
        "session_total_saved": 0.0,
        "total_cost": 0.0,
        "errors": 0,
        "avg_savings_pct": 5.6,
        "model": "claude-sonnet-4-6",
    }
    monkeypatch.setattr(status, "_fetch", _fake_fetch(session))

    status.run_full(proxy_base="http://127.0.0.1:8766")

    out = capsys.readouterr().out
    assert "250 (25.0% compression)" in out
    assert "5.6% compression" not in out


def test_full_status_shows_unknown_when_input_tokens_unavailable(monkeypatch, capsys):
    session = {
        "session_requests": 1,
        "tokens_raw": 0,
        "tokens_saved": 250,
        "cache_read_tokens": 0,
        "session_total_saved": 0.0,
        "total_cost": 0.0,
        "errors": 0,
        "avg_savings_pct": 5.6,
        "model": "claude-sonnet-4-6",
    }
    monkeypatch.setattr(status, "_fetch", _fake_fetch(session))

    status.run_full(proxy_base="http://127.0.0.1:8766")

    out = capsys.readouterr().out
    assert "250 (unknown compression)" in out
    assert "5.6% compression" not in out
