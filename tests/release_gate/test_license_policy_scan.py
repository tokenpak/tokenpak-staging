"""Tests for the license declaration policy scanner."""

import importlib.util
import sys
from pathlib import Path

_MOD_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "release_gate"
    / "license_policy_scan.py"
)
_spec = importlib.util.spec_from_file_location("license_policy_scan", _MOD_PATH)
lps = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = lps
_spec.loader.exec_module(lps)


def _policy() -> lps.LicensePolicy:
    return lps.LicensePolicy(
        canonical_value="Apache-2.0",
        forbidden_values=("MIT", "GPL"),
        allowlist_contexts=("third-party comparison", "historical reference"),
    )


def test_forbidden_json_license_declaration_is_flagged():
    findings = lps.scan_text("sdk/package.json", '"license": "MIT",', _policy())
    assert len(findings) == 1
    assert findings[0].matched == "MIT"


def test_canonical_json_license_declaration_is_clean():
    assert lps.scan_text("sdk/package.json", '"license": "Apache-2.0",', _policy()) == []


def test_comparison_row_is_allowlisted():
    text = "| Another tool | Yes (MIT) | third-party comparison row |"
    assert lps.scan_text("docs/comparison.md", text, _policy()) == []


def test_historical_note_is_allowlisted():
    text = "Historical note: the package was MIT before the current license."
    assert lps.scan_text("CHANGELOG.md", text, _policy()) == []


def test_ledger_loads_required_fields(tmp_path):
    ledger = tmp_path / "license.md"
    ledger.write_text(
        """---
canonical_value: "Apache-2.0"
forbidden_values: ["MIT", "GPL"]
allowlist_contexts:
  - "third-party comparison"
  - "historical reference"
---

# License
""",
        encoding="utf-8",
    )
    policy = lps.load_policy(ledger)
    assert policy.canonical_value == "Apache-2.0"
    assert policy.forbidden_values == ("MIT", "GPL")
    assert len(policy.allowlist_contexts) == 2


def test_cli_fails_on_forbidden_declaration(tmp_path):
    ledger = tmp_path / "decisions" / "ledger"
    ledger.mkdir(parents=True)
    (ledger / "license.md").write_text(
        """---
canonical_value: "Apache-2.0"
forbidden_values: ["MIT"]
allowlist_contexts:
  - "third-party comparison"
---
""",
        encoding="utf-8",
    )
    package = tmp_path / "sdk"
    package.mkdir()
    (package / "package.json").write_text('{"license": "MIT"}\n', encoding="utf-8")
    rc = lps.main(
        [
            "--root",
            str(tmp_path),
            "--ledger",
            "decisions/ledger/license.md",
            "--paths",
            "sdk/package.json",
            "--annotation",
            "none",
        ]
    )
    assert rc == 1


def test_cli_passes_on_canonical_declaration(tmp_path):
    ledger = tmp_path / "decisions" / "ledger"
    ledger.mkdir(parents=True)
    (ledger / "license.md").write_text(
        """---
canonical_value: "Apache-2.0"
forbidden_values: ["MIT"]
allowlist_contexts:
  - "third-party comparison"
---
""",
        encoding="utf-8",
    )
    package = tmp_path / "sdk"
    package.mkdir()
    (package / "package.json").write_text('{"license": "Apache-2.0"}\n', encoding="utf-8")
    rc = lps.main(
        [
            "--root",
            str(tmp_path),
            "--ledger",
            "decisions/ledger/license.md",
            "--paths",
            "sdk/package.json",
            "--annotation",
            "none",
        ]
    )
    assert rc == 0
