"""Unit tests for the version-consistency release gate.

Loaded by path (like the other release_gate tests) so the test does not depend
on a particular ``scripts/`` package layout.
"""
import importlib.util
from pathlib import Path

_MOD_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "release_gate"
    / "check_version_consistency.py"
)
_spec = importlib.util.spec_from_file_location("check_version_consistency", _MOD_PATH)
cvc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cvc)


def test_compare_versions_match_passes():
    assert cvc.compare_versions("1.7.1", "1.7.1") == 0


def test_compare_versions_skew_fails():
    # mutate-to-red: a code/built skew must fail the release path
    assert cvc.compare_versions("1.9.0", "1.7.0") == 1


def test_read_code_version_matches_source():
    code_ver = cvc.read_code_version()
    assert isinstance(code_ver, str) and code_ver
    init_text = (cvc.REPO_ROOT / "tokenpak" / "__init__.py").read_text(encoding="utf-8")
    assert f'__version__ = "{code_ver}"' in init_text


def test_main_passes_when_consistent(monkeypatch):
    monkeypatch.setattr(cvc, "read_code_version", lambda *a, **k: "9.9.9")
    monkeypatch.setattr(cvc, "read_built_version", lambda *a, **k: "9.9.9")
    assert cvc.main() == 0


def test_main_fails_on_skew(monkeypatch):
    monkeypatch.setattr(cvc, "read_code_version", lambda *a, **k: "9.9.9")
    monkeypatch.setattr(cvc, "read_built_version", lambda *a, **k: "1.0.0")
    assert cvc.main() == 1


def test_main_returns_2_when_built_version_undeterminable(monkeypatch):
    monkeypatch.setattr(cvc, "read_code_version", lambda *a, **k: "9.9.9")

    def _raise(*a, **k):
        raise cvc.importlib_metadata.PackageNotFoundError(cvc.DIST_NAME)

    monkeypatch.setattr(cvc, "read_built_version", _raise)
    assert cvc.main() == 2
