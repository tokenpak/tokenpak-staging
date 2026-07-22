"""Source-provenance regressions for the public-API snapshot gate."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "release_gate" / "gen_api_snapshot.py"


def _load_generator():
    spec = importlib.util.spec_from_file_location("api_snapshot_source_provenance", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_script_prefers_own_checkout_over_conflicting_pythonpath(tmp_path):
    fake_root = tmp_path / "conflicting-checkout"
    fake_package = fake_root / "tokenpak"
    fake_package.mkdir(parents=True)
    (fake_package / "__init__.py").write_text(
        "__version__ = '9.9.9'\nWRONG_CHECKOUT_SENTINEL = True\n",
        encoding="utf-8",
    )
    output = tmp_path / "public-api.json"

    env = os.environ.copy()
    env.update({"PYTHONPATH": str(fake_root), "PYTHONDONTWRITEBYTECODE": "1"})
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), "--out", str(output)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    body = output.read_text(encoding="utf-8")
    snapshot = json.loads(body)
    assert snapshot["package_version"] != "9.9.9"
    assert "WRONG_CHECKOUT_SENTINEL" not in body


def test_provenance_check_rejects_package_outside_checkout(tmp_path):
    generator = _load_generator()
    fake_package = SimpleNamespace(__file__=str(tmp_path / "tokenpak" / "__init__.py"))

    with pytest.raises(generator.SourceProvenanceError, match="outside checkout"):
        generator._assert_source_provenance(fake_package)
