# SPDX-License-Identifier: Apache-2.0
"""Self-tests for the identity and public-safety release gates.

This is the false-negative class that leaks internal identifiers to public
OSS: if a gate's event selection or pattern set is weakened, a private path
or internal reference can sail through while CI stays green.

Gate topology on this branch
----------------------------
The pull-request delta gate is implemented in
``identity-language-check.yml`` and chooses event-specific base/head SHAs.
The full public-baseline workflow delegates to the structural scanner in
``scripts/release_gate/public_safety_scan.py``. The local pre-commit hook is a
smaller fast feedback surface and is not treated as equivalent to either CI
gate.

The scanner reports **structural** patterns by design (private home/vault
/tool-state paths, internal fleet phrasing, ``Std 20–39`` references,
keyword-prefixed internal task ids). The exact private identifier list lives
in the internal register, not in public CI (see the scanner's module
docstring), so the fixtures here assert the structural contract rather than a
hardcoded name blocklist.

What this file pins
-------------------
- The baseline workflow invokes ``public_safety_scan.py``.
- The delta workflow covers pull requests and protected-branch pushes and
  fails closed when either event SHA is absent.
- The release pattern set still carries each structural guard label.
- Each forbidden structural surface is flagged by ``scan_text``.
- Allowlisted surfaces (``openclaw``, bare ``fleet``, ``Claude Code``,
  ``Anthropic``, CI-runner paths, sub-``Std 20`` references) are not flagged.
- A weakened standard-reference regex stops catching ``Std 27`` — the
  red-under-mutation signal.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCANNER_PATH = _REPO_ROOT / "scripts" / "release_gate" / "public_safety_scan.py"
_WORKFLOW_PATH = _REPO_ROOT / ".github" / "workflows" / "identity-language-check.yml"
_BASELINE_WORKFLOW_PATH = _REPO_ROOT / ".github" / "workflows" / "public-baseline-scan.yml"


def _load_scanner():
    spec = importlib.util.spec_from_file_location("_public_safety_scan", _SCANNER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before exec: the scanner defines @dataclass types, and on
    # Python 3.12 dataclass processing resolves sys.modules[module.__name__].
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def scanner():
    return _load_scanner()


# ---------------------------------------------------------------------------
# Entrypoint wiring and event coverage.
# ---------------------------------------------------------------------------


def test_public_baseline_workflow_delegates_to_shared_scanner() -> None:
    text = _BASELINE_WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "public_safety_scan.py" in text


def test_delta_workflow_handles_pr_and_push_events_fail_closed() -> None:
    text = _WORKFLOW_PATH.read_text(encoding="utf-8")
    required = {
        "pull_request:",
        "push:",
        "github.event.pull_request.base.sha",
        "github.event.pull_request.head.sha",
        "github.event.before",
        "github.event.after",
        "Event has no usable base SHA",
        "Event has no usable head SHA",
        r"\bStd [0-9]{2}\b",
        r"\.claude/projects",
    }
    missing = {needle for needle in required if needle not in text}
    assert not missing, f"delta gate lost event or pattern guard(s): {sorted(missing)}"


# ---------------------------------------------------------------------------
# The release pattern set still carries every structural guard.
# ---------------------------------------------------------------------------

_REQUIRED_LABELS = frozenset(
    {
        "internal-fleet-phrase",
        "private-home-path",
        "vault-path",
        "private-tool-state-path",
        "internal-task-id-shape",
        "internal-standard-reference",
    }
)


def test_release_pattern_specs_present(scanner) -> None:
    labels = {spec.label for spec in scanner.RELEASE_PATTERN_SPECS}
    missing = _REQUIRED_LABELS - labels
    assert not missing, f"release gate lost structural guard(s): {sorted(missing)}"


# ---------------------------------------------------------------------------
# Forbidden structural surfaces must be flagged (green-on-branch).
# Fixtures are chosen to survive the scanner's own content masking.
# ---------------------------------------------------------------------------

# The vault-path fixture is assembled at runtime rather than embedded as a
# literal: the repo's public/internal boundary guard forbids a hardcoded
# home-rooted vault path anywhere in the tree (even under tests/), while the
# scanner only inspects the string value. We feed the scanner the assembled
# value and keep the source literal-free. The home-path fixture uses a generic
# account name (the boundary guard only forbids real fleet-member home dirs).
_VAULT_PATH_FIXTURE = "manifest at ~/" + "vault" + "/data/index is internal"

_FORBIDDEN_FIXTURES = [
    ("private-home-path", "config path: /home/operator/secret.txt"),
    ("vault-path", _VAULT_PATH_FIXTURE),
    ("private-tool-state-path", "lives under ~/.openclaw/workspace/agent"),
    ("internal-fleet-phrase", "the governor fleet runs nightly"),
    ("internal-task-id-shape", "see task ABC-123 for the follow-up"),
    ("internal-standard-reference", "per Std 27 the boundary applies"),
]


@pytest.mark.parametrize("label,text", _FORBIDDEN_FIXTURES, ids=[f[0] for f in _FORBIDDEN_FIXTURES])
def test_forbidden_surface_is_flagged(scanner, label: str, text: str) -> None:
    findings = scanner.scan_text("docs/example.md", text)
    matched = {f.label for f in findings}
    assert label in matched, f"expected the {label!r} guard to flag {text!r}; got {sorted(matched)}"


# ---------------------------------------------------------------------------
# Allowlisted surfaces must pass (no false positives).
# ---------------------------------------------------------------------------

_ALLOWLISTED_FIXTURES = [
    "openclaw powers the orchestration tool",
    "the fleet is large and healthy",
    "use Claude Code with Anthropic models",
    "CI artifact at /home/runner/work/repo/out",
    "user home /home/user/project is fine",
    "per Std 03 and Std 19 the public rule applies",
]


@pytest.mark.parametrize("text", _ALLOWLISTED_FIXTURES)
def test_allowlisted_surface_passes(scanner, text: str) -> None:
    findings = scanner.scan_text("docs/example.md", text)
    assert findings == [], (
        f"allowlisted surface {text!r} must not be flagged; got "
        f"{[(f.label, f.text) for f in findings]}"
    )


# ---------------------------------------------------------------------------
# Red-under-mutation — a weakened standard-reference regex stops catching it.
# Proves the structural assertions above are sensitive to a gate weakening
# rather than passing vacuously.
# ---------------------------------------------------------------------------


def test_weakened_standard_ref_regex_misses_std_27(scanner) -> None:
    sample = "per Std 27 the boundary applies"

    # Real gate catches it.
    real = scanner.scan_text("docs/example.md", sample)
    assert any(f.label == "internal-standard-reference" for f in real)

    # A weakened variant that only covers Std 30-39 no longer catches Std 27.
    weak_spec = scanner.PatternSpec(
        "internal-standard-ref",
        "internal-standard-reference",
        r"\bStd\s+3[0-9]\b",
    )
    weak_patterns = [(weak_spec, re.compile(weak_spec.regex))]
    weak = scanner.scan_text("docs/example.md", sample, patterns=weak_patterns)
    assert weak == [], "weakened regex unexpectedly still matched Std 27"
