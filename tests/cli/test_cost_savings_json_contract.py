"""JSON contract + optional-dependency resilience for CLI value commands.

Covers two guarantees for the cost/savings value commands:

* ``tokenpak cost --json`` and ``tokenpak savings --json`` emit a single
  parseable JSON document on stdout, with no first-run/welcome/progress prose
  wrapped around it, and ``savings`` reports an explicit no-data state rather
  than a confident zero when no receipts exist.
* ``tokenpak savings`` (and importing the telemetry query package) does not
  traceback when the optional FastAPI extra is absent.

The end-to-end cases run the real CLI in a subprocess against a clean ``HOME``
so the stdout-purity guarantee is exercised exactly as a machine consumer would
see it. FastAPI absence is simulated with a ``sitecustomize`` import blocker so
the test is hermetic regardless of whether FastAPI happens to be installed.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

# tests/cli/test_*.py -> repo root is two levels up.
REPO_ROOT = Path(__file__).resolve().parents[2]

WELCOME_MARKER = "Welcome to TokenPak"


def _write_fastapi_blocker(directory: Path) -> Path:
    """Write a ``sitecustomize`` that makes ``import fastapi`` fail."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "sitecustomize.py").write_text(
        "import builtins\n"
        "_real_import = builtins.__import__\n"
        "\n"
        "def _blocked(name, *args, **kwargs):\n"
        "    if name == 'fastapi' or name.startswith('fastapi.'):\n"
        "        raise ModuleNotFoundError(\"No module named 'fastapi'\")\n"
        "    return _real_import(name, *args, **kwargs)\n"
        "\n"
        "builtins.__import__ = _blocked\n"
    )
    return directory


def _run_cli(
    args,
    *,
    home: Path,
    blocker_dir: Path | None = None,
    extra_env: dict[str, str] | None = None,
):
    """Run ``python -m tokenpak <args>`` with an isolated HOME.

    The worktree under test is prepended to ``PYTHONPATH`` so it shadows any
    editable install. When ``blocker_dir`` is given, a FastAPI import blocker is
    placed ahead of it so the command runs as if FastAPI were not installed.
    """
    env = dict(os.environ)
    env["HOME"] = str(home)
    path_parts = [str(REPO_ROOT)]
    if blocker_dir is not None:
        path_parts.insert(0, str(blocker_dir))
    if env.get("PYTHONPATH"):
        path_parts.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(path_parts)
    if extra_env:
        env.update(extra_env)
    # Avoid inheriting a port that points at someone else's running proxy.
    if not extra_env or "TOKENPAK_PORT" not in extra_env:
        env.pop("TOKENPAK_PORT", None)
    return subprocess.run(
        [sys.executable, "-m", "tokenpak", *args],
        env=env,
        capture_output=True,
        text=True,
    )


def _create_monitor_db(path: Path, *, cache_origin: str) -> None:
    """Create a minimal monitor.db with one cache-heavy request row."""
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            CREATE TABLE requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                model TEXT NOT NULL,
                input_tokens INTEGER,
                output_tokens INTEGER,
                estimated_cost REAL,
                compressed_tokens INTEGER,
                cache_read_tokens INTEGER,
                cache_creation_tokens INTEGER DEFAULT 0,
                cache_origin TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO requests (
                timestamp, model, input_tokens, output_tokens, estimated_cost,
                compressed_tokens, cache_read_tokens, cache_origin
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                "claude-haiku-4-5",
                100_000,
                0,
                0.0,
                40_000,
                60_000,
                cache_origin,
            ),
        )
        conn.commit()
    finally:
        conn.close()


# --- P0-B: JSON contract ----------------------------------------------------


def test_cost_json_is_single_parseable_document(tmp_path):
    res = _run_cli(["cost", "--json"], home=tmp_path / "home")
    assert res.returncode == 0, res.stderr
    data = json.loads(res.stdout)  # raises if stdout is not pure JSON
    assert data["section"] == "cost"
    assert "spent_usd" in data
    assert "budgets" in data
    assert WELCOME_MARKER not in res.stdout


def test_savings_json_no_data_is_explicit_not_confident_zero(tmp_path):
    res = _run_cli(["savings", "--json"], home=tmp_path / "home")
    assert res.returncode == 0, res.stderr
    data = json.loads(res.stdout)
    assert data["section"] == "savings"
    assert data["available"] is False
    assert data["state"] == "no_data"
    # A confident zero would surface savings_amount: 0.0 — the no-data contract
    # must omit the numeric metrics instead.
    assert "savings_amount" not in data
    assert WELCOME_MARKER not in res.stdout


def test_savings_json_with_data_reports_metrics():
    from tokenpak._cli_core import _savings_json_payload
    from tokenpak.telemetry.query_models import SavingsReport

    report = SavingsReport(
        total_cost=10.0,
        estimated_without_compression=15.0,
        savings_amount=5.0,
        savings_pct=33.3333,
        cache_hit_rate=0.5,
    )
    payload = _savings_json_payload(report, days=30)
    assert payload["available"] is True
    assert payload["savings_amount"] == 5.0
    assert payload["actual_cost"] == 10.0
    assert payload["baseline_cost"] == 15.0
    assert payload["attribution"]["model"] == "conservative_tokenpak_caused_savings"
    assert "state" not in payload


def test_savings_json_observes_client_cache_without_crediting_it(tmp_path):
    db = tmp_path / "monitor.db"
    _create_monitor_db(db, cache_origin="client")

    res = _run_cli(
        ["savings", "--json"],
        home=tmp_path / "home",
        extra_env={"TOKENPAK_DB": str(db), "TOKENPAK_PORT": "59231"},
    )
    assert res.returncode == 0, res.stderr
    data = json.loads(res.stdout)

    assert data["available"] is True
    assert data["cache_hit_rate"] > 0
    assert data["savings_amount"] == 0.0
    assert data["actual_cost"] == data["baseline_cost"]
    assert data["attribution"]["provider_or_client_cache"] == "observed_not_credited"


def test_cost_json_payload_includes_by_model(monkeypatch):
    from tokenpak import _cli_core

    class _FakeTracker:
        def total_spent(self, period):
            return 1.5

        def by_model_summary(self, period):
            return [
                {
                    "model": "claude-3-haiku",
                    "requests": 4,
                    "tokens_input": 100,
                    "tokens_output": 200,
                    "cost_usd": 1.5,
                }
            ]

        def get_status(self, period):
            return None

    monkeypatch.setattr(_cli_core, "_monitor_db_cost", lambda period: 0.0)
    monkeypatch.setattr(_cli_core, "_proxy_get", lambda path: None)

    args = type("A", (), {"by_model": True, "month": False, "week": False})()
    payload = _cli_core._cost_json_payload(args, _FakeTracker(), "daily")
    assert payload["section"] == "cost"
    assert payload["spent_usd"] == 1.5
    assert payload["by_model"][0]["model"] == "claude-3-haiku"
    assert payload["live_session"] is None
    assert payload["budgets"] == {}


# --- P0-C: FastAPI optional-dependency resilience ---------------------------


def test_query_package_imports_without_fastapi(tmp_path):
    blocker = _write_fastapi_blocker(tmp_path / "blocker")
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(blocker), str(REPO_ROOT), env.get("PYTHONPATH", "")]
    )
    # Sanity: the blocker actually makes FastAPI unavailable.
    sanity = subprocess.run(
        [sys.executable, "-c", "import fastapi"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert sanity.returncode != 0

    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "from tokenpak.telemetry.query import get_savings_report, api;"
            "print('API_IS_NONE', api is None);"
            "get_savings_report(days=30);"
            "print('OK')",
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    assert probe.returncode == 0, probe.stderr
    assert "Traceback" not in probe.stderr
    assert "API_IS_NONE True" in probe.stdout
    assert "OK" in probe.stdout


def test_savings_cli_no_traceback_without_fastapi(tmp_path):
    res = _run_cli(
        ["savings"],
        home=tmp_path / "home",
        blocker_dir=_write_fastapi_blocker(tmp_path / "blocker"),
    )
    assert res.returncode == 0, f"stdout={res.stdout!r} stderr={res.stderr!r}"
    assert "Traceback" not in res.stderr
    assert "ModuleNotFoundError" not in res.stderr


def test_savings_json_no_traceback_without_fastapi(tmp_path):
    res = _run_cli(
        ["savings", "--json"],
        home=tmp_path / "home",
        blocker_dir=_write_fastapi_blocker(tmp_path / "blocker"),
    )
    assert res.returncode == 0, res.stderr
    data = json.loads(res.stdout)
    assert data["section"] == "savings"
    assert data["available"] is False
    assert "Traceback" not in res.stderr
