"""Home-resolver regression tests for telemetry pipeline ledger side effects."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WATCHDOG_STUB = """
import sys
import types

watchdog = types.ModuleType("watchdog")
events = types.ModuleType("watchdog.events")
observers = types.ModuleType("watchdog.observers")
events.FileSystemEventHandler = object
observers.Observer = object
watchdog.events = events
watchdog.observers = observers
sys.modules.setdefault("watchdog", watchdog)
sys.modules.setdefault("watchdog.events", events)
sys.modules.setdefault("watchdog.observers", observers)
"""


def _run_python(code: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "TOKENPAK_HOME": str(tmp_path / "tpk-home"),
        "TOKENPAK_SHADOW_ENABLED": "true",
        "PYTHONPATH": str(ROOT),
    }
    script = WATCHDOG_STUB + "\n" + textwrap.dedent(code)
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
    )


def test_import_telemetry_pipeline_creates_no_cwd_ledger(tmp_path):
    result = _run_python("import tokenpak.telemetry.pipeline", tmp_path)

    assert result.returncode == 0, result.stderr
    assert not (tmp_path / ".tokenpak").exists()
    assert not (tmp_path / "tpk-home" / "routing_ledger.db").exists()


def test_shadow_event_writes_routing_ledger_under_tokenpak_home(tmp_path):
    result = _run_python(
        """
        from tokenpak.telemetry.pipeline import TelemetryPipeline

        class Storage:
            def insert_event(self, event): pass
            def insert_usage(self, usage): pass
            def insert_cost(self, cost): pass

        TelemetryPipeline(Storage()).process({
            "model": "gpt-4o",
            "query": "hello",
            "response": "ok",
            "usage": {"prompt_tokens": 2, "completion_tokens": 1},
        })
        """,
        tmp_path,
    )

    assert result.returncode == 0, result.stderr
    assert not (tmp_path / ".tokenpak").exists()
    assert (tmp_path / "tpk-home" / "routing_ledger.db").exists()
