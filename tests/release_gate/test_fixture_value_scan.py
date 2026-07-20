"""Regression tests for the delta-style test-fixture VALUE scan.

Exercises ``scripts/release_gate/check_release_leaks.py --fixture-values``
end-to-end via its CLI:

  * a NEW forbidden fixture value in a changed test file FAILS the gate;
  * a legacy occurrence in a file NOT on the changed list PASSES
    (delta-style historical-debt tolerance);
  * docstrings and comments are prose, not values, and never trip;
  * data-fixture files (json/…) are scanned on full content;
  * ``tests/release_gate/`` (the gate's own fixture surface) is excluded;
  * functional-literal masks from the shared register still apply to values;
  * non-test paths on the list are out of scope for this mode.

These fixtures intentionally contain the forbidden strings. They live under
``tests/release_gate/`` (excluded from the delta gate, the fixture-value scan,
and the shipped artifact), so they cannot trip any gate themselves.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCANNER = REPO_ROOT / "scripts" / "release_gate" / "check_release_leaks.py"


def _write(root: Path, relpath: str, content: str) -> None:
    p = root / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def _run_fixture_scan(root: Path, changed: list[str]) -> subprocess.CompletedProcess:
    listfile = root / "changed-test-files.txt"
    listfile.write_text("".join(f"{c}\n" for c in changed), encoding="utf-8")
    return subprocess.run(
        [
            sys.executable,
            str(SCANNER),
            "--fixture-values",
            str(listfile),
            "--root",
            str(root),
        ],
        capture_output=True,
        text=True,
    )


def test_new_forbidden_fixture_value_on_changed_file_is_blocked(tmp_path):
    # The exact leak class from the corrective audit: an internal agent name
    # as a header-fixture VALUE in a changed test file.
    _write(
        tmp_path,
        "tests/proxy/test_headers.py",
        'def test_x():\n    headers = {"X-Tokenpak-Agent": "Trix"}\n    assert headers\n',
    )
    res = _run_fixture_scan(tmp_path, ["tests/proxy/test_headers.py"])
    assert res.returncode == 1, f"forbidden fixture value must fail:\n{res.stdout}"
    assert "tests/proxy/test_headers.py" in res.stdout
    assert "test-fixture value" in res.stdout


def test_legacy_occurrence_on_unchanged_file_is_tolerated(tmp_path):
    # Historical debt: the forbidden value exists in the tree, but the file is
    # NOT on the changed list — the gate must not scan it (delta tolerance).
    _write(
        tmp_path,
        "tests/legacy/test_old.py",
        'AGENT = "Suki"\n',
    )
    _write(
        tmp_path,
        "tests/proxy/test_new.py",
        'def test_y():\n    assert "agent-a"\n',
    )
    res = _run_fixture_scan(tmp_path, ["tests/proxy/test_new.py"])
    assert res.returncode == 0, f"unchanged legacy file must be tolerated:\n{res.stdout}"


def test_docstrings_and_comments_are_not_values(tmp_path):
    # Prose surfaces in Python test files are not fixture VALUES; the delta
    # content gate governs prose on shipped surfaces, this mode does not.
    _write(
        tmp_path,
        "tests/proxy/test_prose.py",
        '"""Module notes: reviewed per Std 20 process."""\n'
        "# internal note: TSR-1234\n"
        "def test_z():\n"
        '    """Docstring mentioning Sue."""\n'
        "    assert True\n",
    )
    res = _run_fixture_scan(tmp_path, ["tests/proxy/test_prose.py"])
    assert res.returncode == 0, f"docstrings/comments must not trip:\n{res.stdout}"


def test_data_fixture_file_values_are_scanned(tmp_path):
    _write(
        tmp_path,
        "tests/fixtures/agents.json",
        '{"agent": "Cali"}\n',
    )
    res = _run_fixture_scan(tmp_path, ["tests/fixtures/agents.json"])
    assert res.returncode == 1, f"data-fixture value must fail:\n{res.stdout}"
    assert "tests/fixtures/agents.json" in res.stdout


def test_release_gate_fixture_dir_is_excluded(tmp_path):
    # The gate's own regression fixtures intentionally contain register
    # strings; they are excluded exactly like the scanner files themselves.
    _write(
        tmp_path,
        "tests/release_gate/test_selffixture.py",
        'LEAK = "/home/sue/.cache"\n',
    )
    res = _run_fixture_scan(tmp_path, ["tests/release_gate/test_selffixture.py"])
    assert res.returncode == 0, f"release-gate fixture dir must be excluded:\n{res.stdout}"


def test_functional_literal_masks_apply_to_values(tmp_path):
    # Shared-register masks (e.g. the config-filename literal) apply to
    # fixture values too — same engine, same allowlists.
    _write(
        tmp_path,
        "tests/integrations/test_config.py",
        'CONFIG = "openclaw.json"\nHDR = "x-tokenpak-fleet"\n',
    )
    res = _run_fixture_scan(tmp_path, ["tests/integrations/test_config.py"])
    assert res.returncode == 0, f"masked functional literals must pass:\n{res.stdout}"


def test_non_test_paths_on_list_are_ignored(tmp_path):
    # This mode is scoped to test surfaces; the main delta content scan
    # already covers everything else.
    _write(
        tmp_path,
        "tokenpak/core/notes.py",
        'REVIEWER = "Sue"\n',
    )
    res = _run_fixture_scan(tmp_path, ["tokenpak/core/notes.py"])
    assert res.returncode == 0, f"non-test paths are out of scope here:\n{res.stdout}"


def test_unparseable_python_does_not_crash_the_gate(tmp_path):
    # An unparseable Python test cannot execute and already fails the test
    # jobs; the fixture-value gate skips it rather than erroring.
    _write(
        tmp_path,
        "tests/proxy/test_broken.py",
        "def broken(:\n",
    )
    res = _run_fixture_scan(tmp_path, ["tests/proxy/test_broken.py"])
    assert res.returncode == 0, f"syntax errors must not crash the gate:\n{res.stdout}"


def test_deleted_file_on_list_is_skipped(tmp_path):
    res = _run_fixture_scan(tmp_path, ["tests/proxy/test_gone.py"])
    assert res.returncode == 0, f"missing files must be skipped:\n{res.stdout}"
