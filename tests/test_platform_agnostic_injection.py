# SPDX-License-Identifier: Apache-2.0
"""Automatic context must reach every route whose policy declares it.

Route policy has declared ``"vault_injection": "json_inject"`` for the OpenClaw
and SDK routes since those routes existed, and nothing ever performed it. The
only pipeline invocation lived in the byte-preserved branch, so automatic
context reached one client and silently skipped the rest — while configuration
said otherwise.

The stage implemented the mode correctly the whole time. It was simply never
called.

The defect class is **"declared but never invoked"** — the configuration-level
twin of "computed but never surfaced". A test that exercised the stage directly
would have passed throughout, because the stage was never the broken part. So
the guard below asserts the *wiring*, not the stage.
"""

from __future__ import annotations

import inspect

import pytest

from tokenpak.proxy.pipeline import stage_vault_injection
from tokenpak.proxy.request import ProxyRequest
from tokenpak.proxy.route_policy import get_policy

ORIGINAL = b'{"model":"x","messages":[{"role":"user","content":"hi"}]}'
INJECTED = b'{"model":"x","messages":[{"role":"user","content":"CONTEXT hi"}]}'


@pytest.fixture
def fake_inject(monkeypatch):
    """Stand in for vault retrieval; the vault itself is not under test here."""
    import tokenpak.proxy.config as proxy_config
    import tokenpak.proxy.vault_bridge as vb

    # The shipped master switch is deliberately OFF. These tests exercise the
    # route wiring beneath that gate, so opt in explicitly rather than weakening
    # the production default.
    monkeypatch.setattr(proxy_config, "VAULT_INJECTION_ENABLED", True)

    def _fake(body, adapter=None, request=None):
        return INJECTED, 412, ["decisions/auth.md", "notes/api.md"], "CONTEXT"

    monkeypatch.setattr(vb, "_inject_vault_context_with_text", _fake)
    return _fake


def _req(body=ORIGINAL):
    return ProxyRequest(
        method="POST", url="https://example.invalid/v1/messages", headers={}, body=body
    )


# ---------------------------------------------------------------------------
# The stage was never the broken part — confirm that, so the guard below is
# unambiguous about where the defect actually lived.
# ---------------------------------------------------------------------------


def test_json_inject_applies_the_body_in_stage(fake_inject):
    """json_inject mutates the request itself; it does not defer to byte_restore."""
    req, stage = stage_vault_injection(_req(), {"vault_injection": "json_inject"})

    assert not stage.skipped
    assert req.body == INJECTED
    assert stage.details["injected_tokens"] == 412
    assert stage.details["injected_sources"] == ["decisions/auth.md", "notes/api.md"]


def test_byte_splice_does_not_apply_the_body(fake_inject):
    """byte_splice defers the mutation to the byte-restore stage.

    Guards the 99.86% of live traffic that rides this path: the platform-agnostic
    change must not alter how the flagship client is handled.
    """
    req, stage = stage_vault_injection(_req(), {"vault_injection": "byte_splice"})

    assert not stage.skipped
    assert req.body == ORIGINAL, "byte_splice must not mutate the body in-stage"
    assert stage.details["injected_tokens"] == 412


@pytest.mark.parametrize(
    "policy", [{"vault_injection": "disabled"}, {}], ids=["explicit", "absent"]
)
def test_disabled_skips_with_a_reason(policy, fake_inject):
    _req_out, stage = stage_vault_injection(_req(), policy)
    assert stage.skipped
    assert stage.skip_reason == "disabled_or_empty"


# ---------------------------------------------------------------------------
# The actual regression guard: policy must not declare what nothing performs.
# ---------------------------------------------------------------------------


def _declared_injection_modes() -> set[str]:
    """Every non-disabled vault_injection mode any route policy declares."""
    from tokenpak.proxy.route_policy import ROUTE_POLICIES

    return {
        mode
        for policy in ROUTE_POLICIES.values()
        if (mode := policy.get("vault_injection", "disabled")) and mode != "disabled"
    }


def test_route_policy_declares_both_modes():
    """Sanity: the policy table is still the thing this test is guarding."""
    assert _declared_injection_modes() == {"byte_splice", "json_inject"}


def test_every_route_policy_resolves():
    """`get_policy` must return a usable policy for every declared route."""
    from tokenpak.proxy.route_policy import ROUTE_POLICIES

    for route in ROUTE_POLICIES:
        assert "vault_injection" in get_policy(route)


def test_every_declared_injection_mode_has_a_call_site():
    """No route may declare an injection mode that no code path performs.

    This is the invariant the original defect violated. It is asserted against
    the request handler's source because the failure was never visible in the
    stage's behaviour — the stage worked; nobody called it.
    """
    import tokenpak.proxy.server as server_mod

    src = inspect.getsource(server_mod)
    handler = src[src.index("def _proxy_to_inner") :]

    # The byte-preserved branch runs the full pipeline; the json path calls the
    # vault stage directly (the other stages are handled inline on that path).
    assert "process_request" in handler or "_pipeline_run" in handler, (
        "byte_splice has no injection call site in the request handler"
    )
    assert "stage_vault_injection" in handler, (
        "json_inject is declared in route policy but no call site exists in the "
        "request handler — automatic context silently skips those routes"
    )


def test_injection_is_reachable_outside_the_byte_preserved_branch():
    """The json_inject call must not be nested inside `if _is_byte_preserved:`.

    A call site that exists but sits behind the byte-preserved guard reproduces
    the original bug exactly while satisfying a naive presence check — so this
    walks the AST and asserts which *branch* the call lives in, rather than
    merely that it appears somewhere in the file.
    """
    import ast

    import tokenpak.proxy.server as server_mod

    tree = ast.parse(inspect.getsource(server_mod))

    def calls_stage(node) -> bool:
        return any(
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "stage_vault_injection"
            for n in ast.walk(node)
        )

    guards = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.If)
        and isinstance(n.test, ast.Name)
        and n.test.id == "_is_byte_preserved"
        and n.orelse
    ]
    assert guards, "`if _is_byte_preserved: ... else: ...` not found — test needs updating"

    in_else = any(any(calls_stage(stmt) for stmt in g.orelse) for g in guards)
    in_body = any(any(calls_stage(stmt) for stmt in g.body) for g in guards)

    assert in_else, (
        "json_inject is declared in route policy but vault injection is not called in "
        "the non-byte-preserved branch — automatic context silently skips those routes"
    )
    assert not in_body, (
        "the byte-preserved branch should run the full pipeline, not call the vault stage directly"
    )
