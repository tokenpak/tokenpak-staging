"""Tier-1 release-check gate tests — a passing and a deliberately-failing
fixture per deterministic gate.

The module is loaded by path so the test does not depend on a particular
package layout for ``scripts/``. Lives under tests/ (which the release-check
leak gate and the identity scan both skip), so the deliberate leak/literal
fixtures below do not flag the test file itself.
"""
import importlib.util
import sys
from pathlib import Path

_MOD = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "release_check"
    / "release_check.py"
)
_spec = importlib.util.spec_from_file_location("release_check_under_test", _MOD)
rc = importlib.util.module_from_spec(_spec)
# Register before exec so the module's @dataclass can resolve cls.__module__
# via sys.modules (the standard importlib-by-path requirement).
sys.modules[_spec.name] = rc
_spec.loader.exec_module(rc)


# --- maturity ----------------------------------------------------------------
def _write_pkg(root, classifier="4 - Beta", status="Beta", license_ok=True):
    (root / "README.md").write_text(
        f"# TokenPak\n**Status:** {status} — APIs may change.\n"
        "Licensed under the Apache License 2.0.\n",
        encoding="utf-8",
    )
    lic = "Apache License\nVersion 2.0, January 2004\n" if license_ok else "MIT License\n"
    (root / "LICENSE").write_text(lic, encoding="utf-8")
    (root / "pyproject.toml").write_text(
        "[project]\nname = \"tokenpak\"\n"
        "classifiers = [\n"
        f"    \"Development Status :: {classifier}\",\n"
        "    \"License :: OSI Approved :: Apache Software License\",\n"
        "]\n",
        encoding="utf-8",
    )


def test_maturity_pass(tmp_path):
    _write_pkg(tmp_path, classifier="4 - Beta", status="Beta")
    assert rc.gate_maturity(tmp_path).ok


def test_maturity_pass_when_readme_declares_no_marker(tmp_path):
    # Adapted 2026-07-19 (ADAPT-INTO-CURRENT-REPAIR): match-if-declared — a
    # README with no maturity marker contradicts nothing; only a declared
    # marker that mismatches the classifier (or an unknown marker) fails.
    _write_pkg(tmp_path, classifier="5 - Production/Stable", status="Beta")
    (tmp_path / "README.md").write_text(
        "# TokenPak\nLicensed under the Apache License 2.0.\n", encoding="utf-8"
    )
    assert rc.gate_maturity(tmp_path).ok


def test_maturity_fail_production_stable(tmp_path):
    # the anchoring incident: README Beta but classifier Production/Stable
    _write_pkg(tmp_path, classifier="5 - Production/Stable", status="Beta")
    r = rc.gate_maturity(tmp_path)
    assert not r.ok


# --- license -----------------------------------------------------------------
def test_license_pass(tmp_path):
    _write_pkg(tmp_path, license_ok=True)
    assert rc.gate_license(tmp_path).ok


def test_license_fail_non_apache(tmp_path):
    _write_pkg(tmp_path, license_ok=False)
    assert not rc.gate_license(tmp_path).ok


# --- leak (delta-style core) -------------------------------------------------
def test_leak_scan_clean():
    pats = rc.load_leak_patterns()
    assert rc.scan_leaks("docs/x.md", "TokenPak routes requests to your provider.", pats) == []


def test_leak_scan_flags_ticket_and_path():
    pats = rc.load_leak_patterns()
    hits = rc.scan_leaks("docs/x.md", "Tracked in TSR-7; logs under /home/sue/run.", pats)
    assert hits  # both a ticket-ID and a private path


# --- help-verbs (pure core) --------------------------------------------------
def test_help_verbs_all_resolve():
    assert rc.check_help_verbs([("serve", True), ("config", True)]) == []


def test_help_verbs_detects_phantom():
    assert rc.check_help_verbs([("serve", True), ("ghost", False)]) == ["ghost"]


def test_live_cli_has_no_phantom_verbs():
    # integration: the real parser must expose no unresolved verb. Guards the
    # ancestor-dispatch case (e.g. `openclaw refresh-models` has no own func but
    # is dispatched by the openclaw handler — must count as resolved).
    verbs = rc.collect_cli_verbs()
    assert verbs, "expected the live CLI to expose verbs"
    assert rc.check_help_verbs(verbs) == []


# --- tokenpak-literal regression --------------------------------------------
def test_tokenpak_literal_pass_when_baselined(tmp_path):
    pkg = tmp_path / "tokenpak"
    pkg.mkdir()
    (pkg / "legacy.py").write_text('HOME = "~/.tokenpak/config.yaml"\n', encoding="utf-8")
    r = rc.gate_tokenpak_literal(tmp_path, allowed={"tokenpak/legacy.py"})
    assert r.ok


def test_tokenpak_literal_fail_on_new_offender(tmp_path):
    pkg = tmp_path / "tokenpak"
    pkg.mkdir()
    (pkg / "newmod.py").write_text('p = "~/.tokenpak/new.db"\n', encoding="utf-8")
    r = rc.gate_tokenpak_literal(tmp_path, allowed=set())
    assert not r.ok


# --- orchestrator exit code --------------------------------------------------
def test_main_single_gate_exit_zero_on_clean(tmp_path):
    _write_pkg(tmp_path, classifier="4 - Beta", status="Beta")
    assert rc.main(["--root", str(tmp_path), "--gate", "maturity"]) == 0


def test_main_single_gate_exit_one_on_incident(tmp_path):
    _write_pkg(tmp_path, classifier="5 - Production/Stable", status="Beta")
    assert rc.main(["--root", str(tmp_path), "--gate", "maturity"]) == 1
