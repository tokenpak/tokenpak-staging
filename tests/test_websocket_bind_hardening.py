"""
Tests for the WebSocket helper loopback-bind hardening.

The ``tokenpak.proxy.websocket`` helper forwards client-supplied
``x-api-key`` / ``authorization`` headers upstream and has no localhost
client-IP gate, so it must default to a loopback-only bind and only expose a
non-loopback interface when the operator explicitly opts in via
``TOKENPAK_WS_UNSAFE_BIND``.

These are pure unit tests of the bind-host resolver; they never start a real
listener and never touch the network.

Covers:
  1. test_default_bind_is_loopback - default resolves to loopback
  2. test_is_loopback_bind_classification - loopback vs non-loopback addrs
  3. test_nonloopback_clamped_without_optin - wildcard refused -> 127.0.0.1
  4. test_nonloopback_allowed_with_optin - wildcard honoured with flag
  5. test_unsafe_flag_truthy_parsing - env truthiness parsing
"""
from __future__ import annotations

import pytest

from tokenpak.proxy.websocket import (
    _is_loopback_bind,
    _resolve_ws_bind_host,
    _ws_unsafe_bind_enabled,
)

# ---------------------------------------------------------------------------
# Default posture
# ---------------------------------------------------------------------------

def test_default_bind_is_loopback(monkeypatch):
    """With the default (loopback) LISTEN_ADDRESS, the WS bind stays loopback."""
    monkeypatch.setattr("tokenpak.proxy.config.LISTEN_ADDRESS", "127.0.0.1")
    monkeypatch.delenv("TOKENPAK_WS_UNSAFE_BIND", raising=False)
    assert _resolve_ws_bind_host() == "127.0.0.1"


# ---------------------------------------------------------------------------
# Address classification
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "addr",
    ["127.0.0.1", "::1", "localhost", "127.0.0.5", "127.1.2.3", "  127.0.0.1  ", "LOCALHOST"],
)
def test_is_loopback_bind_true(addr):
    assert _is_loopback_bind(addr) is True


@pytest.mark.parametrize(
    "addr",
    ["0.0.0.0", "::", "192.168.1.17", "10.0.0.1", "", None, "example.com"],
)
def test_is_loopback_bind_false(addr):
    assert _is_loopback_bind(addr) is False


# ---------------------------------------------------------------------------
# Non-loopback gating
# ---------------------------------------------------------------------------

def test_nonloopback_clamped_without_optin(monkeypatch):
    """A wildcard LISTEN_ADDRESS is refused and clamped to loopback by default."""
    monkeypatch.setattr("tokenpak.proxy.config.LISTEN_ADDRESS", "0.0.0.0")
    monkeypatch.delenv("TOKENPAK_WS_UNSAFE_BIND", raising=False)
    assert _resolve_ws_bind_host() == "127.0.0.1"


def test_nonloopback_allowed_with_optin(monkeypatch):
    """The explicit opt-in flag honours a non-loopback bind."""
    monkeypatch.setattr("tokenpak.proxy.config.LISTEN_ADDRESS", "0.0.0.0")
    monkeypatch.setenv("TOKENPAK_WS_UNSAFE_BIND", "1")
    assert _resolve_ws_bind_host() == "0.0.0.0"


# ---------------------------------------------------------------------------
# Opt-in flag parsing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "on", " On "])
def test_unsafe_flag_truthy(monkeypatch, val):
    monkeypatch.setenv("TOKENPAK_WS_UNSAFE_BIND", val)
    assert _ws_unsafe_bind_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "no", "", "off", "maybe"])
def test_unsafe_flag_falsy(monkeypatch, val):
    monkeypatch.setenv("TOKENPAK_WS_UNSAFE_BIND", val)
    assert _ws_unsafe_bind_enabled() is False


def test_unsafe_flag_unset_is_false(monkeypatch):
    monkeypatch.delenv("TOKENPAK_WS_UNSAFE_BIND", raising=False)
    assert _ws_unsafe_bind_enabled() is False
