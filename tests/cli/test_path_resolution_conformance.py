# SPDX-License-Identifier: Apache-2.0
"""TOKENPAK_HOME must isolate writes, and preview --json must stay JSON.

Both defects were found by an adversarial review on 2026-07-26. The first
caused real data loss: `TOKENPAK_HOME=/tmp/x tokenpak setup --yes` wrote
nothing to /tmp/x and overwrote the operator's live ~/.tokenpak/config.yaml
with no backup. The file-boundary standard names TOKENPAK_HOME an operator
override for sandboxes, so honouring a config found outside it is a
conformance violation, not a design choice.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def _run(args, home, tp_home=None):
    env = {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "TERM": "dumb",
        "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
    }
    if tp_home is not None:
        env["TOKENPAK_HOME"] = str(tp_home)
    return subprocess.run(
        [sys.executable, "-m", "tokenpak", *args], capture_output=True, text=True, env=env
    )


class TestHomeOverrideIsolatesWrites:
    def test_setup_writes_inside_the_override_only(self):
        """setup must not retarget its write to a config found outside the override."""
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            legacy = home / ".tokenpak"
            legacy.mkdir(parents=True)
            decoy = legacy / "config.yaml"
            decoy.write_text("proxy:\n  port: 9999\n")
            before = decoy.read_text()

            sandbox = Path(td) / "sandbox"
            _run(["setup", "--yes", "--profile", "balanced"], home, tp_home=sandbox)

            # The live/legacy config is untouched...
            assert decoy.read_text() == before, (
                "setup escaped TOKENPAK_HOME and rewrote real config"
            )
            # ...and the sandbox actually received one.
            assert list(sandbox.glob("config*")), "setup wrote nothing into TOKENPAK_HOME"


class TestPreviewJsonContract:
    def test_input_guard_still_emits_json(self):
        """--json emits JSON on every path, including refusals."""
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            home.mkdir()
            target = Path(td) / "conv.json"
            target.write_text(json.dumps([{"role": "user", "content": "hi"}]))

            # A path passed positionally is refused — but must refuse in JSON.
            r = _run(["preview", "--json", str(target)], home)
            assert r.returncode != 0
            payload = json.loads(r.stdout)  # must not be prose
            assert payload["state"] == "error"
            assert payload["reason"]
