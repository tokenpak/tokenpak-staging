# SPDX-License-Identifier: Apache-2.0
"""The bare-invocation chooser must describe each mode truthfully.

The prompt this replaced offered "Hard (purge everything)". `--hard` does not
purge everything: it deliberately retains the session journal, the budget
history and the saved capsules. A destructive prompt that overstates its reach
is worse than a terse one, because a user who believes it thinks data is
already lost.
"""

from __future__ import annotations

import pytest

from tokenpak.cli.commands import uninstall as U


@pytest.mark.parametrize(
    "answer,expected",
    [
        ("", "soft"),
        ("1", "soft"),
        ("s", "soft"),
        ("soft", "soft"),
        ("2", "hard"),
        ("h", "hard"),
        ("hard", "hard"),
    ],
)
def test_chooser_maps_answers(answer, expected, monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda *_a: answer)
    assert U._choose_mode() == expected


@pytest.mark.parametrize("answer", ["q", "quit", "nonsense", "3"])
def test_unrecognised_and_quit_cancel(answer, monkeypatch, capsys):
    """Anything not understood cancels rather than picking a destructive default."""
    monkeypatch.setattr("builtins.input", lambda *_a: answer)
    assert U._choose_mode() is None


@pytest.mark.parametrize("exc", [EOFError, KeyboardInterrupt])
def test_non_answer_cancels(exc, monkeypatch, capsys):
    def _raise(*_a):
        raise exc

    monkeypatch.setattr("builtins.input", _raise)
    assert U._choose_mode() is None


def test_chooser_does_not_claim_hard_removes_everything(monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda *_a: "")
    U._choose_mode()
    out = capsys.readouterr().out
    assert "purge everything" not in out
    # It must say plainly what survives.
    assert "KEPT" in out
    assert "journal" in out and "budget" in out and "capsules" in out
    # And which option is the safe one.
    assert "recommended" in out


def test_chooser_offers_a_cancel_path(monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda *_a: "")
    U._choose_mode()
    assert "[q] Cancel" in capsys.readouterr().out
