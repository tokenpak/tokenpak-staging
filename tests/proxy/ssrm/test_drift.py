"""Unit tests for tokenpak.proxy.ssrm.drift."""

from __future__ import annotations

import pytest

from tokenpak.proxy.ssrm.drift import drift_for_body, drift_score, jaccard


def test_jaccard_identical_strings_score_one():
    assert jaccard("alpha beta gamma", "alpha beta gamma") == 1.0


def test_jaccard_disjoint_score_zero():
    assert jaccard("alpha beta", "gamma delta") == 0.0


def test_jaccard_partial_overlap():
    # alpha, beta, gamma, delta — intersection = {alpha, beta} (size 2), union size 4 → 0.5
    s = jaccard("alpha beta gamma", "alpha beta delta")
    assert s == pytest.approx(0.5, abs=0.001)


def test_drift_score_unknown_algorithm_falls_back_to_jaccard():
    # Unknown algorithm should not crash; falls back to jaccard.
    s = drift_score("alpha beta", "alpha beta", algorithm="some-future-algo")
    assert s == 1.0


def test_drift_for_body_against_anchor():
    """When the body's last user turn matches an anchor, drift = 1.0."""
    body = {"messages": [{"role": "user", "content": "alpha beta gamma"}]}
    s = drift_for_body(body, "alpha beta gamma")
    assert s == 1.0
    s2 = drift_for_body(body, "completely different other words")
    assert s2 == 0.0
