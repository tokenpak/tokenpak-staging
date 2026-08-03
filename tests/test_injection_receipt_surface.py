# SPDX-License-Identifier: Apache-2.0
"""The vault-injection receipt must reach a surface a user can read.

Regression coverage for a defect where the receipt was computed and discarded:
the vault stage recorded ``injected_tokens`` / ``injected_sources`` onto
``StageResult.details``, and nothing consumed them. The monitor row took
hardcoded ``0`` / ``""``, the live-session counters had no writer at all, and
``tokenpak status`` therefore reported "0 across 0 requests" permanently while
injection was working correctly on every eligible request.

The defect class is **"value computed, never surfaced"**, so these tests assert
at the *render* site and at the *persistence* site. A test that only checked the
producer would have passed throughout the entire period the bug existed — which
is precisely what happened during review.
"""

from __future__ import annotations

import pytest

from tokenpak.cli.commands.status import _format_injected_sources
from tokenpak.proxy.server import (
    _MAX_SESSION_SOURCES,
    _read_injection_receipt,
    _record_injection_in_session,
)


class _Stage:
    def __init__(self, name, skipped=False, details=None):
        self.name = name
        self.skipped = skipped
        self.details = details or {}


class _Result:
    def __init__(self, stages):
        self.stages = stages


def _injecting_result(tokens=412, sources="decisions/auth.md,notes/api.md"):
    return _Result(
        [
            _Stage("cache_poison_removal"),
            _Stage(
                "vault_injection",
                details={"injected_tokens": tokens, "injected_sources": sources},
            ),
        ]
    )


# ---------------------------------------------------------------------------
# Reading the receipt off the pipeline
# ---------------------------------------------------------------------------


def test_receipt_is_read_from_the_vault_stage():
    tokens, sources = _read_injection_receipt(_injecting_result())
    assert tokens == 412
    assert sources == "decisions/auth.md,notes/api.md"


def test_receipt_accepts_sources_as_a_sequence():
    """``compile_injection`` returns source refs as a sequence, not a string."""
    result = _Result(
        [
            _Stage(
                "vault_injection",
                details={"injected_tokens": 7, "injected_sources": ["a.md", "b.md"]},
            )
        ]
    )
    assert _read_injection_receipt(result) == (7, "a.md,b.md")


@pytest.mark.parametrize(
    "result",
    [
        _Result([]),
        _Result([_Stage("compaction")]),
        _Result([_Stage("vault_injection", skipped=True, details={"injected_tokens": 99})]),
    ],
    ids=["no-stages", "no-vault-stage", "vault-stage-skipped"],
)
def test_non_injecting_requests_report_a_measured_zero(result):
    assert _read_injection_receipt(result) == (0, "")


def test_malformed_details_do_not_raise():
    """Telemetry must never break a request, including on garbage input."""
    result = _Result(
        [
            _Stage(
                "vault_injection",
                details={"injected_tokens": "not-a-number", "injected_sources": None},
            )
        ]
    )
    assert _read_injection_receipt(result) == (0, "")


# ---------------------------------------------------------------------------
# Reaching the live session — the counters `tokenpak status` reads
# ---------------------------------------------------------------------------


@pytest.fixture
def session(monkeypatch):
    from tokenpak.core.runtime import proxy as runtime_proxy

    fresh: dict = {}
    monkeypatch.setattr(runtime_proxy, "SESSION", fresh, raising=False)
    return fresh


def test_injection_accumulates_onto_the_session(session):
    _record_injection_in_session(412, "decisions/auth.md,notes/api.md")
    _record_injection_in_session(88, "decisions/auth.md,other.md")

    assert session["injected_tokens"] == 500
    assert session["injection_hits"] == 2
    # De-duplicated, insertion-ordered.
    assert session["injected_source_names"] == ["decisions/auth.md", "notes/api.md", "other.md"]


def test_non_injecting_request_does_not_count_as_a_hit(session):
    _record_injection_in_session(0, "")
    assert session.get("injected_tokens", 0) == 0
    assert session.get("injection_hits", 0) == 0


def test_session_source_list_is_bounded(session):
    for i in range(_MAX_SESSION_SOURCES * 3):
        _record_injection_in_session(1, f"source-{i}.md")
    assert len(session["injected_source_names"]) == _MAX_SESSION_SOURCES
    # The cap drops the oldest, so the most recent injection is still nameable.
    assert f"source-{_MAX_SESSION_SOURCES * 3 - 1}.md" in session["injected_source_names"]


# ---------------------------------------------------------------------------
# Reaching the user — the render site
# ---------------------------------------------------------------------------


def test_sources_are_named_at_the_render_site():
    lines = _format_injected_sources(["decisions/auth.md", "notes/api.md"])
    rendered = "\n".join(lines)
    assert "decisions/auth.md" in rendered
    assert "notes/api.md" in rendered


def test_render_accepts_the_persisted_string_form():
    """The monitor column stores a comma-separated string."""
    rendered = "\n".join(_format_injected_sources("a.md,b.md"))
    assert "a.md" in rendered and "b.md" in rendered


@pytest.mark.parametrize("empty", ["", [], None, 0], ids=["str", "list", "none", "int"])
def test_render_emits_nothing_when_there_is_nothing_to_name(empty):
    assert _format_injected_sources(empty) == []


def test_render_truncates_but_says_so():
    """A silently shortened list reads as a complete one."""
    names = [f"src-{i}.md" for i in range(12)]
    lines = _format_injected_sources(names)
    rendered = "\n".join(lines)

    assert "src-0.md" in rendered
    assert "src-11.md" not in rendered
    assert "+7 more" in rendered


# ---------------------------------------------------------------------------
# The end-to-end property the defect violated
# ---------------------------------------------------------------------------


def test_injecting_request_is_nameable_end_to_end(session):
    """Pipeline result -> session -> rendered output, with no hardcoded default.

    This is the assertion whose absence let the defect ship: every individual
    layer was correct, and the value still never reached a human.
    """
    tokens, sources = _read_injection_receipt(_injecting_result())
    _record_injection_in_session(tokens, sources)

    assert session["injected_tokens"] > 0, "token count never reached the session"
    assert session["injection_hits"] == 1

    rendered = "\n".join(_format_injected_sources(session["injected_source_names"]))
    assert rendered.strip(), "injection happened but nothing was rendered to the user"
    assert "decisions/auth.md" in rendered
