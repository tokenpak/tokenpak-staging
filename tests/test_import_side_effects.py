# SPDX-License-Identifier: Apache-2.0
"""Top-level import side-effect regression tests."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_HEAVY_MODULES = ("torch", "llmlingua", "litellm", "sentence_transformers")


def test_import_tokenpak_does_not_load_heavy_extras_or_write_files(tmp_path):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    script = """
import json
import os
import pathlib
import sys

root = pathlib.Path(os.environ["TOKENPAK_IMPORT_SIDE_EFFECT_ROOT"])
before = sorted(str(p.relative_to(root)) for p in root.rglob("*"))
import tokenpak
after = sorted(str(p.relative_to(root)) for p in root.rglob("*"))
print(json.dumps({
    "version": tokenpak.__version__,
    "heavy_loaded": [m for m in %r if m in sys.modules],
    "before": before,
    "after": after,
}))
""" % (list(_HEAVY_MODULES),)
    env = {
        **os.environ,
        "HOME": str(sandbox / "home"),
        "TOKENPAK_HOME": str(sandbox / "tpk-home"),
        "PYTHONPATH": str(_REPO_ROOT),
        "PYTHONDONTWRITEBYTECODE": "1",
        "TOKENPAK_IMPORT_SIDE_EFFECT_ROOT": str(sandbox),
    }

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=_REPO_ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["version"]
    assert payload["heavy_loaded"] == []
    assert payload["before"] == []
    assert payload["after"] == []
