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


def test_non_interactive_error_does_not_repeat_the_untruth(monkeypatch, capsys):
    """The same claim lived in the piped/scripted path, not just the prompt.

    Fixing it in the interactive chooser and leaving it verbatim one function
    over would make the command tell two different stories about the same flag,
    and the scripted message is the one every CI log carries.
    """
    monkeypatch.setattr(U.sys.stdin, "isatty", lambda: False, raising=False)
    monkeypatch.setattr(U.sys.stdout, "isatty", lambda: False, raising=False)
    rc = U.run_uninstall()
    assert rc == 2
    err = capsys.readouterr().err
    assert "purge everything" not in err
    assert "journal" in err and "budget" in err and "capsules" in err


@pytest.mark.parametrize("destroyed", ["Paks", "templates", "cards", "license"])
def test_both_surfaces_name_the_user_content_hard_destroys(destroyed, monkeypatch, capsys):
    """`--hard` deletes saved Paks, templates, cards and the license.

    Describing that as "configuration, caches and databases" is wrong in the
    dangerous direction: a user who trusts it keeps their Paks in their head as
    surviving, and they do not. Both the prompt and the scripted error must say
    so, or the command tells two stories again.
    """
    monkeypatch.setattr("builtins.input", lambda *_a: "")
    U._choose_mode()
    assert destroyed.lower() in capsys.readouterr().out.lower()

    monkeypatch.setattr(U.sys.stdin, "isatty", lambda: False, raising=False)
    monkeypatch.setattr(U.sys.stdout, "isatty", lambda: False, raising=False)
    U.run_uninstall()
    assert destroyed.lower() in capsys.readouterr().err.lower()
