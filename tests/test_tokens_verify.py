# SPDX-License-Identifier: Apache-2.0
"""Independent token-count verification tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tokenpak.telemetry import tokens_verify


class _FakeEncoder:
    def __init__(self, count: int) -> None:
        self.count = count

    def encode(self, text: str) -> list[int]:
        assert text
        return list(range(self.count))


def test_verify_token_count_reports_absolute_and_relative_divergence(monkeypatch) -> None:
    module = SimpleNamespace(get_encoding=lambda name: _FakeEncoder(7))
    monkeypatch.setattr(tokens_verify, "import_module", lambda name: module)

    result = tokens_verify.verify_token_count("some text", reported_count=5)

    assert result.reported_count == 5
    assert result.independent_count == 7
    assert result.absolute_divergence == 2
    assert result.relative_divergence == pytest.approx(0.4)


def test_empty_text_has_zero_counts(monkeypatch) -> None:
    module = SimpleNamespace(get_encoding=lambda name: _FakeEncoder(99))
    monkeypatch.setattr(tokens_verify, "import_module", lambda name: module)

    result = tokens_verify.verify_token_count("", reported_count=0)

    assert result.independent_count == 0
    assert result.absolute_divergence == 0
    assert result.relative_divergence == 0.0


def test_missing_tiktoken_has_specific_install_message(monkeypatch) -> None:
    def missing(name: str) -> object:
        raise ModuleNotFoundError(name)

    monkeypatch.setattr(tokens_verify, "import_module", missing)

    with pytest.raises(tokens_verify.TokenizerUnavailableError, match=r"tokenpak\[tokens\]"):
        tokens_verify.verify_token_count("some text", reported_count=2)


def test_negative_reported_count_is_rejected() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        tokens_verify.verify_token_count("some text", reported_count=-1)
