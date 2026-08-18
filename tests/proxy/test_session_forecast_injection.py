# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the optional client-return session-economics decoration.

Covers: the feature flag defaults off; marker wrap/detect/strip is
idempotent and a byte-identical no-op when absent; response decoration
touches only the client-facing copy; request scrub only ever activates on
assistant-authored content and is a no-op when the marker is absent.
"""

from __future__ import annotations

import json

from tokenpak.proxy import session_forecast_injection as inj


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("TOKENPAK_SESSION_FORECAST_INJECTION", raising=False)
    monkeypatch.setattr("tokenpak.core.config.load_config", lambda: {}, raising=False)
    assert inj.is_injection_enabled() is False


def test_env_var_enables_and_disables(monkeypatch):
    monkeypatch.setenv("TOKENPAK_SESSION_FORECAST_INJECTION", "1")
    assert inj.is_injection_enabled() is True
    monkeypatch.setenv("TOKENPAK_SESSION_FORECAST_INJECTION", "0")
    assert inj.is_injection_enabled() is False


def test_config_file_key_enables_when_env_unset(monkeypatch):
    monkeypatch.delenv("TOKENPAK_SESSION_FORECAST_INJECTION", raising=False)
    monkeypatch.setattr(
        "tokenpak.core.config.load_config",
        lambda: {"session_forecast_injection": {"enabled": True}},
    )
    assert inj.is_injection_enabled() is True


def test_wrap_contains_and_strip_round_trip():
    original = "just the assistant's answer."
    wrapped = original + inj.wrap_marker("session economics: cost $0.42")
    assert inj.contains_marker(wrapped)
    assert not inj.contains_marker(original)
    stripped = inj.strip_markers(wrapped)
    assert stripped == original


def test_strip_is_idempotent_and_noop_when_absent():
    plain = "no marker here at all"
    assert inj.strip_markers(plain) is plain  # identity, not just equality
    wrapped = plain + inj.wrap_marker("x")
    once = inj.strip_markers(wrapped)
    twice = inj.strip_markers(once)
    assert once == twice == plain


def test_marker_survives_across_multiple_appends_and_strips_all():
    """Defensive: even if two envelopes were ever concatenated, strip
    removes every occurrence, never leaving a partial marker behind."""
    text = "answer" + inj.wrap_marker("a") + inj.wrap_marker("b")
    stripped = inj.strip_markers(text)
    assert stripped == "answer"
    assert inj.MARKER_OPEN_PREFIX not in stripped


# ---------------------------------------------------------------------------
# Response decoration — client copy only
# ---------------------------------------------------------------------------


def _resp(text: str) -> bytes:
    return json.dumps(
        {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
        }
    ).encode()


def test_decorate_response_body_appends_to_last_text_block():
    original = _resp("the answer")
    decorated = inj.decorate_response_body(original, "session economics: cost $1.00")
    assert decorated != original
    data = json.loads(decorated)
    text = data["content"][-1]["text"]
    assert text.startswith("the answer")
    assert inj.contains_marker(text)
    assert inj.strip_markers(text) == "the answer"


def test_decorate_response_body_never_mutates_input_object():
    original = _resp("the answer")
    before = bytes(original)
    inj.decorate_response_body(original, "line")
    assert original == before  # same bytes, unmutated


def test_decorate_response_body_is_noop_on_bad_shape():
    not_json = b"not json at all"
    assert inj.decorate_response_body(not_json, "line") is not_json
    no_content = json.dumps({"type": "message"}).encode()
    assert inj.decorate_response_body(no_content, "line") is no_content
    empty_blocks = json.dumps({"content": []}).encode()
    assert inj.decorate_response_body(empty_blocks, "line") is empty_blocks


def test_maybe_decorate_response_noop_when_disabled(monkeypatch):
    monkeypatch.setenv("TOKENPAK_SESSION_FORECAST_INJECTION", "0")
    original = _resp("the answer")
    out = inj.maybe_decorate_response(original, session_id="s1", db_path="/nonexistent")
    assert out is original


def test_maybe_decorate_response_noop_without_session_id(monkeypatch):
    monkeypatch.setenv("TOKENPAK_SESSION_FORECAST_INJECTION", "1")
    original = _resp("the answer")
    out = inj.maybe_decorate_response(original, session_id="", db_path="/nonexistent")
    assert out is original


def test_maybe_decorate_response_fails_open_on_build_error(monkeypatch):
    """Even enabled with a resolvable session id, any internal failure
    building/rendering the economics summary must forward bytes unchanged."""
    monkeypatch.setenv("TOKENPAK_SESSION_FORECAST_INJECTION", "1")

    def _boom(*a, **kw):
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr("tokenpak.proxy.forecast_endpoint._build_session_economics_response", _boom)
    original = _resp("the answer")
    out = inj.maybe_decorate_response(original, session_id="s1", db_path="/nonexistent")
    assert out is original


# ---------------------------------------------------------------------------
# Request-side scrub
# ---------------------------------------------------------------------------


def _req(messages: list[dict]) -> bytes:
    return json.dumps({"model": "m", "max_tokens": 32, "messages": messages}).encode()


def test_scrub_is_identity_object_when_marker_absent():
    body = _req([{"role": "user", "content": "hi"}])
    assert inj.scrub_request_body(body) is body


def test_scrub_removes_marker_from_assistant_string_content():
    decorated_text = "prior answer" + inj.wrap_marker("session economics: cost $1.00")
    body = _req(
        [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": decorated_text},
            {"role": "user", "content": "follow up"},
        ]
    )
    out = inj.scrub_request_body(body)
    data = json.loads(out)
    assert data["messages"][1]["content"] == "prior answer"
    assert inj.MARKER_OPEN_PREFIX not in out.decode()


def test_scrub_removes_marker_from_assistant_content_blocks():
    decorated_text = "prior answer" + inj.wrap_marker("session economics: cost $1.00")
    body = _req(
        [
            {
                "role": "assistant",
                "content": [{"type": "text", "text": decorated_text}],
            }
        ]
    )
    out = inj.scrub_request_body(body)
    data = json.loads(out)
    assert data["messages"][0]["content"][0]["text"] == "prior answer"


def test_scrub_never_touches_user_role_content():
    """A marker string a user typed themselves is left alone — scrub only
    ever strips content from assistant-authored messages."""
    body = _req([{"role": "user", "content": "look: " + inj.wrap_marker("fake")}])
    out = inj.scrub_request_body(body)
    assert out is body


def test_scrub_is_idempotent_across_repeated_turns():
    decorated_text = "prior answer" + inj.wrap_marker("line")
    body = _req([{"role": "assistant", "content": decorated_text}])
    once = inj.scrub_request_body(body)
    twice = inj.scrub_request_body(once)
    assert once == twice
    assert json.loads(once)["messages"][0]["content"] == "prior answer"


def test_scrub_fails_open_on_unparseable_body_containing_marker_bytes():
    garbage = b"{not json but contains [TP-ECON marker text"
    assert inj.scrub_request_body(garbage) is garbage
