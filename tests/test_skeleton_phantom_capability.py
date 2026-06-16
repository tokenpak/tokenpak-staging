"""Regression tests for the skeleton capability surface — post-implementation form.

Context: the "code skeleton" feature is wired into config, profiles, the proxy
injection path, and the doctor/status capability surface. Historically the core
extractor module (``tokenpak.skeleton_extractor``) did not exist, so the
feature was a silent no-op; the Phase-1 truth patch
(``p0-skeleton-feature-truth-patch-phantom-capability``) made the capability
surface report from a real import probe, emit a diagnostic when the extractor
was missing, and removed the unbacked savings claim.

Phase 2 (``p2-skeleton-extractor-implementation``) shipped the real extractor,
so this file now asserts the *inverse* of the Phase-1 absence-invariants:

- the extractor is importable and the probe reports **available**,
- enabled intent + available capability reports **active**,
- the injection path performs **real extraction** (signatures/docstrings kept,
  bodies elided) with **no** missing-extractor diagnostic,
- intent still gates: disabled means inactive and a byte-identical path,
- the anti-phantom guarantee survives: no unbacked savings percentage in the
  injection sources (claims must be measured — see the benchmark in
  ``tests/test_skeleton_extractor.py``).
"""

from __future__ import annotations

import logging
from pathlib import Path


def test_extractor_module_present_and_callable():
    """Phase 2 landed: the extractor ships with the package. Absence is now a
    packaging failure, not an acceptable degraded state."""
    from tokenpak.skeleton_extractor import extract_skeleton

    assert callable(extract_skeleton)


def test_skeleton_available_probe_true_with_extractor_present():
    """The capability probe reflects real importability — and the extractor is
    really importable now."""
    from tokenpak.proxy.config import skeleton_available

    assert skeleton_available() is True


def test_skeleton_reported_active_iff_enabled(monkeypatch):
    """Post-impl inverse of the anti-phantom invariant: with the extractor
    present, `active` follows the intent flag exactly — True when enabled,
    False when disabled (capability alone must not report active)."""
    import tokenpak.proxy.config as cfg

    monkeypatch.setattr(cfg, "SKELETON_ENABLED", True, raising=False)
    assert cfg.skeleton_active() is True

    monkeypatch.setattr(cfg, "SKELETON_ENABLED", False, raising=False)
    assert cfg.skeleton_active() is False


def test_skeletonize_block_extracts_without_missing_diagnostic(monkeypatch, caplog):
    """The injection path now performs real extraction: signature + docstring
    survive, the body is elided, and the Phase-1 missing-extractor diagnostic
    does NOT fire."""
    import tokenpak.proxy.config as cfg
    import tokenpak.vault.chunk_shaping as cs

    monkeypatch.setattr(cfg, "SKELETON_ENABLED", True, raising=False)
    # Reset the one-shot diagnostic latch for a deterministic assertion.
    monkeypatch.setattr(cs, "_SKELETON_EXTRACTOR_MISSING", False, raising=False)

    code = (
        "def foo(x: int) -> int:\n"
        '    """Add one, twice."""\n'
        "    y = x + 1\n"
        "    z = y + 1\n"
        "    return z\n"
    )
    with caplog.at_level(logging.WARNING, logger="tokenpak.skeleton"):
        out = cs._skeletonize_block(code, ".py")

    # Real extraction: signature + docstring kept, body elided.
    assert "def foo(x: int) -> int:" in out
    assert '"""Add one, twice."""' in out
    assert "y = x + 1" not in out
    assert "return z" not in out

    # Capability status reports available, with no missing-extractor latch ...
    status = cs.skeleton_runtime_status()
    assert status["enabled"] is True
    assert status["available"] is True
    assert status["extractor_missing_observed"] is False
    # ... and no missing-extractor log line.
    assert not any("extractor unavailable" in r.message for r in caplog.records)


def test_non_skeleton_injection_path_byte_identical(monkeypatch):
    """Intent still gates: with the feature disabled the injection path is
    byte-identical (unchanged from the Phase-1 guarantee)."""
    import tokenpak.proxy.config as cfg
    import tokenpak.vault.chunk_shaping as cs

    monkeypatch.setattr(cfg, "SKELETON_ENABLED", False, raising=False)
    blocks = "Some prose.\n\n```python\ndef foo(x):\n    return x + 1\n```\n"
    assert cs._inject_skeleton_into_blocks(blocks) == blocks


def test_injection_skeletonizes_supported_fence(monkeypatch):
    """With skeleton enabled, a supported code fence is skeletonized in place:
    signature preserved, body elided, fence structure intact."""
    import tokenpak.proxy.config as cfg
    import tokenpak.vault.chunk_shaping as cs

    monkeypatch.setattr(cfg, "SKELETON_ENABLED", True, raising=False)
    blocks = (
        "Some prose.\n\n"
        "```python\n"
        "def foo(x):\n"
        '    """Doc."""\n'
        "    a = x + 1\n"
        "    b = a * 2\n"
        "    return b\n"
        "```\n"
    )
    out = cs._inject_skeleton_into_blocks(blocks)
    assert "Some prose." in out
    assert "def foo(x):" in out
    assert '"""Doc."""' in out
    assert "a = x + 1" not in out
    assert "return b" not in out
    # Fence structure intact.
    assert out.count("```") == 2


def test_injection_noop_fences_pass_through_byte_identical(monkeypatch):
    """Fences the extractor does not change must pass through byte-identical —
    the historical fence re-assembly quirk (stray trailing newline before the
    closing fence) is fixed with the real extractor."""
    import tokenpak.proxy.config as cfg
    import tokenpak.vault.chunk_shaping as cs

    monkeypatch.setattr(cfg, "SKELETON_ENABLED", True, raising=False)
    # Unsupported language fence + a supported fence with nothing to elide.
    blocks = (
        "Prose.\n\n"
        "```ruby\ndef foo\n  1\nend\n```\n\n"
        "```python\nX = 1\n```\n"
    )
    assert cs._inject_skeleton_into_blocks(blocks) == blocks


def test_no_unbacked_savings_percentage_in_injection_source():
    """No live code may assert a skeleton savings percentage that isn't backed
    by a passing benchmark test (the Phase-1 removed '70-90% reduction' claim
    must not creep back into the injection sources)."""
    import tokenpak.proxy.vault_bridge as vb
    import tokenpak.vault.chunk_shaping as cs

    for mod in (vb, cs):
        src = Path(mod.__file__).read_text(encoding="utf-8")
        assert "70-90% reduction" not in src
        assert "70-90%" not in src
