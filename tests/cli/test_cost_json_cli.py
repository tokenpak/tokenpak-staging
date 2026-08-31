# SPDX-License-Identifier: Apache-2.0
"""`tokenpak cost --json` must emit a machine-readable document.

Sibling reporting commands (`savings`, `status`, `doctor`) accept `--json`;
`cost` previously rejected the flag at the parser (exit 2). These tests pin
the aligned contract:

* `--json` emits one well-formed JSON document and nothing else.
* `--week` / `--month` compose with `--json` — the selected period is
  reflected in the document.
* `--by-model --json` returns the per-model breakdown.
* `--json` and `--export-csv` are mutually exclusive — competing output
  modes are rejected at the parser, not silently resolved.
* `--help` advertises the flag.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_cost(home: Path, *argv: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["HOME"] = str(home)
    env.pop("TOKENPAK_HOME", None)
    env.pop("TOKENPAK_DB", None)
    env.pop("TOKENPAK_MONITOR_DB", None)
    env["TOKENPAK_PORT"] = "8899"  # nothing listens here
    return subprocess.run(
        [sys.executable, "-m", "tokenpak.cli", "cost", *argv],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
        timeout=90,
    )


def _fresh_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    (home / ".tpk").mkdir(parents=True)
    (home / ".tpk" / ".seen_intro").touch()  # suppress the first-run banner
    return home


def _seed_monitor_db(home: Path, rows: list[tuple]) -> None:
    """Create a monitor store (the proxy's live request log) with *rows*."""
    conn = sqlite3.connect(home / ".tpk" / "monitor.db")
    conn.execute(
        "CREATE TABLE requests (timestamp TEXT, model TEXT, input_tokens INT, "
        "output_tokens INT, estimated_cost REAL)"
    )
    conn.executemany("INSERT INTO requests VALUES (?,?,?,?,?)", rows)
    conn.commit()
    conn.close()


def _seed_budget_db(home: Path, rows: list[tuple]) -> None:
    """Create a budget store (per-request spend log) with *rows*.

    Each row is (request_id, timestamp, model, cost_usd, tokens_input,
    tokens_output, agent).
    """
    conn = sqlite3.connect(home / ".tpk" / "budget.db")
    conn.execute(
        "CREATE TABLE tp_spend (request_id TEXT NOT NULL, timestamp TEXT NOT NULL, "
        "model TEXT NOT NULL DEFAULT '', cost_usd REAL NOT NULL DEFAULT 0, "
        "tokens_input INTEGER NOT NULL DEFAULT 0, tokens_output INTEGER NOT NULL DEFAULT 0, "
        "agent TEXT NOT NULL DEFAULT '')"
    )
    conn.execute("CREATE UNIQUE INDEX idx_spend_request_id ON tp_spend(request_id)")
    conn.executemany(
        "INSERT INTO tp_spend VALUES (?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()


def _is_json(text: str) -> bool:
    try:
        json.loads(text)
    except json.JSONDecodeError:
        return False
    return True


# ---------------------------------------------------------------------------
# --json default summary
# ---------------------------------------------------------------------------


def test_json_with_data_emits_wellformed_document(tmp_path):
    import datetime

    home = _fresh_home(tmp_path)
    today = datetime.date.today().isoformat()
    _seed_monitor_db(home, [(today, "claude-opus-5", 1000, 500, 0.0234)])

    result = _run_cost(home, "--json")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == result.stdout.strip(), "sanity"
    payload = json.loads(result.stdout)
    assert payload["section"] == "cost"
    assert payload["period"] == "daily"
    assert payload["available"] is True
    assert payload["spent_usd"] == 0.0234
    for key in ("live_session", "budget"):
        assert key in payload, f"missing key: {key}"


def test_json_output_contains_only_the_document(tmp_path):
    """stdout must be exactly the JSON document — no banners, no prose."""
    home = _fresh_home(tmp_path)

    result = _run_cost(home, "--json")

    assert result.returncode == 0, result.stderr
    # A single json.loads over the full stripped stdout must succeed, i.e.
    # there is nothing before or after the document.
    json.loads(result.stdout)


def test_json_no_store_reports_unavailable_not_zero(tmp_path):
    home = _fresh_home(tmp_path)

    result = _run_cost(home, "--json")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["available"] is False
    assert payload["spent_usd"] is None


def test_json_empty_store_reports_a_measured_zero(tmp_path):
    home = _fresh_home(tmp_path)
    _seed_monitor_db(home, [])

    result = _run_cost(home, "--json")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["available"] is True
    assert payload["spent_usd"] == 0.0


# ---------------------------------------------------------------------------
# --week / --month compose with --json
# ---------------------------------------------------------------------------


def test_json_respects_month_flag(tmp_path):
    import datetime

    home = _fresh_home(tmp_path)
    today = datetime.date.today().isoformat()
    _seed_monitor_db(home, [(today, "claude-opus-5", 1000, 500, 0.05)])

    result = _run_cost(home, "--json", "--month")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["period"] == "monthly"
    assert payload["spent_usd"] == 0.05


def test_json_respects_week_flag(tmp_path):
    import datetime

    home = _fresh_home(tmp_path)
    today = datetime.date.today().isoformat()
    _seed_monitor_db(home, [(today, "claude-opus-5", 1000, 500, 0.05)])

    result = _run_cost(home, "--json", "--week")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["period"] == "weekly"


# ---------------------------------------------------------------------------
# --by-model --json
# ---------------------------------------------------------------------------


def test_json_by_model_emits_breakdown(tmp_path):
    import datetime

    home = _fresh_home(tmp_path)
    now = datetime.datetime.now().isoformat()
    _seed_budget_db(
        home,
        [
            ("req-1", now, "claude-opus-5", 0.02, 1000, 500, ""),
            ("req-2", now, "claude-sonnet-5", 0.01, 2000, 800, ""),
        ],
    )

    result = _run_cost(home, "--json", "--by-model")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["section"] == "cost"
    assert payload["period"] == "daily"
    models = {row["model"] for row in payload["by_model"]}
    assert models == {"claude-opus-5", "claude-sonnet-5"}
    assert payload["total_cost_usd"] == 0.03


def test_json_by_model_empty_is_wellformed(tmp_path):
    home = _fresh_home(tmp_path)

    result = _run_cost(home, "--json", "--by-model")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["by_model"] == []
    assert payload["total_cost_usd"] == 0.0


# ---------------------------------------------------------------------------
# --json conflicts with --export-csv
# ---------------------------------------------------------------------------


def test_json_and_export_csv_are_mutually_exclusive(tmp_path):
    home = _fresh_home(tmp_path)

    result = _run_cost(home, "--json", "--export-csv")

    assert result.returncode == 2
    assert "not allowed with argument" in result.stderr


# ---------------------------------------------------------------------------
# human output unchanged without the flag
# ---------------------------------------------------------------------------


def test_human_output_unchanged(tmp_path):
    import datetime

    home = _fresh_home(tmp_path)
    today = datetime.date.today().isoformat()
    _seed_monitor_db(home, [(today, "claude-opus-5", 1000, 500, 0.0234)])

    result = _run_cost(home)

    assert result.returncode == 0, result.stderr
    assert "TokenPak Cost Summary" in result.stdout
    assert "$0.0234" in result.stdout
    assert not _is_json(result.stdout), "human-readable output must not be a JSON document"


# ---------------------------------------------------------------------------
# help surface
# ---------------------------------------------------------------------------


def test_help_advertises_json_flag(tmp_path):
    home = _fresh_home(tmp_path)

    result = _run_cost(home, "--help")

    assert result.returncode == 0, result.stderr
    assert "--json" in result.stdout
