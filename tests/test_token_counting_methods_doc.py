# SPDX-License-Identifier: Apache-2.0
"""Contract tests for the public token-counting method map."""

from __future__ import annotations

from pathlib import Path


def _section(text: str, heading: str, next_heading: str) -> str:
    return text.split(heading, 1)[1].split(next_heading, 1)[0]


def test_proxy_estimators_are_documented_as_chars_primary_bytes_fallback():
    """Regression: estimator documentation must match parsed-string behavior."""
    text = Path("docs/token-counting-methods.md").read_text(encoding="utf-8")
    chars = _section(text, "## Where chars ÷ 4 is load-bearing", "## Where bytes ÷ 4")
    bytes_section = _section(text, "## Where bytes ÷ 4 is load-bearing", "## Out of scope")

    for estimator in ("_estimate_tokens_from_body()", "_estimate_tokens()"):
        assert estimator in chars
        assert estimator in bytes_section
    assert "primary parsed-string paths" in bytes_section
    assert "fallback when JSON parsing or recognized-structure traversal fails" in bytes_section
