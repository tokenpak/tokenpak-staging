# SPDX-License-Identifier: Apache-2.0
"""Vault injection has a master switch, and it is OFF by default.

Injection admits retrieved vault content into a request before it reaches the
provider. Two controls that cover every other request path do **not** yet cover
injected content: the outbound DLP secret scan and the spend guard both evaluate
the body *before* injection runs. Until they evaluate the body that is actually
sent, enabling injection would ship vault content to providers unscanned and
untracked against cost caps.

The switch exists so that repairing injection — which is an *activation* of a
path that has never functioned, not a bug fix — cannot silently turn it on
everywhere at once.

These tests pin the default. A change to the default is a governance decision
and should have to break a test that says so.
"""

from __future__ import annotations

import pytest

from tokenpak.proxy import config as cfg_mod
from tokenpak.proxy.pipeline import stage_vault_injection
from tokenpak.proxy.request import ProxyRequest

BODY = b'{"model":"claude-sonnet-4-6","messages":[{"role":"user","content":"hi"}]}'


def _req():
    return ProxyRequest(
        method="POST", url="https://api.anthropic.com/v1/messages", headers={}, body=BODY
    )


@pytest.fixture
def injection_enabled(monkeypatch):
    monkeypatch.setattr(cfg_mod, "VAULT_INJECTION_ENABLED", True, raising=False)


@pytest.fixture(autouse=True)
def switch_off(monkeypatch):
    """Behaviour tests set the switch explicitly; they must not inherit host state.

    Otherwise a developer or CI runner with the flag legitimately enabled sees
    this file fail for a reason that has nothing to do with the code.
    """
    monkeypatch.setattr(cfg_mod, "VAULT_INJECTION_ENABLED", False, raising=False)


@pytest.fixture
def fake_retrieval(monkeypatch):
    """Stub retrieval so the stage would do something if it were allowed to."""
    import tokenpak.proxy.vault_bridge as vb

    monkeypatch.setattr(
        vb,
        "inject_vault_context",
        lambda body, adapter=None, request=None: (body, 412, ["decisions/auth.md"]),
        raising=False,
    )


# ---------------------------------------------------------------------------
# The default
# ---------------------------------------------------------------------------


def test_shipped_default_is_off():
    """The SHIPPED default must be OFF.

    Asserts the *declared* default, not the resolved runtime value. An earlier
    version asserted `cfg_mod.VAULT_INJECTION_ENABLED is False`, which fails on
    any host that has legitimately enabled the flag — `TOKENPAK_VAULT_INJECTION=1`
    made 5 of 7 tests in this file fail, with diagnostics telling the operator
    "someone changed the default. That is a governance decision" when they had
    merely used the flag for its stated purpose.

    A test must not accuse a correct operator of a governance violation.
    """
    import inspect

    src = inspect.getsource(cfg_mod)
    assert 'features.vault_injection", False' in src, (
        "the shipped default for features.vault_injection is no longer False — "
        "that is a governance decision requiring the DLP and spend-guard ordering "
        "to land first"
    )


def test_disabled_by_default_skips_with_a_distinct_reason(fake_retrieval):
    """The skip must be attributable to the switch, not confused with policy.

    `disabled_or_empty` already means "this route does not want injection".
    A separate reason keeps "the operator turned it off" distinguishable from
    "this route never asked for it" — otherwise the receipt cannot explain
    itself.
    """
    _out, stage = stage_vault_injection(_req(), {"vault_injection": "json_inject"})

    assert stage.skipped
    assert stage.skip_reason == "disabled_by_config"
    assert stage.skip_reason != "disabled_or_empty"


def test_switch_beats_route_policy(fake_retrieval):
    """Route policy asking for injection does not override the master switch."""
    for mode in ("json_inject", "byte_splice"):
        _out, stage = stage_vault_injection(_req(), {"vault_injection": mode})
        assert stage.skipped, f"{mode} ran despite the master switch being off"
        assert stage.skip_reason == "disabled_by_config"


def test_nothing_is_reported_when_disabled(fake_retrieval):
    """A disabled stage must not report injected tokens or sources.

    Guards against the receipt defect class: a stage that is skipped must not
    leave numbers behind that a downstream surface could render as work done.
    """
    _out, stage = stage_vault_injection(_req(), {"vault_injection": "json_inject"})

    assert stage.details.get("injected_tokens", 0) == 0
    assert not stage.details.get("injected_sources")
    assert stage.tokens_delta == 0


def test_body_is_untouched_when_disabled(monkeypatch):
    """The stub must MUTATE, or this test proves nothing.

    An earlier version used a stub that returned the body unchanged — so
    `out.body == BODY` held whether or not the guard existed, and would hold even
    with injection fully working. Review confirmed it passed with the guard
    deleted. A test that cannot fail is not coverage.
    """
    import tokenpak.proxy.vault_bridge as vb

    mutated = b'{"model":"x","messages":[{"role":"user","content":"MUTATED"}]}'
    monkeypatch.setattr(
        vb,
        "inject_vault_context",
        lambda body, adapter=None, request=None: (mutated, 412, ["decisions/auth.md"]),
        raising=False,
    )

    out, stage = stage_vault_injection(_req(), {"vault_injection": "json_inject"})

    assert stage.skip_reason == "disabled_by_config"
    assert out.body == BODY, "the guard did not prevent a mutating injector from running"
    assert out.body != mutated


# ---------------------------------------------------------------------------
# The switch actually gates — not just present
# ---------------------------------------------------------------------------


def test_enabling_the_switch_lets_the_stage_run(injection_enabled, fake_retrieval):
    """With the switch on, the stage proceeds past the guard.

    Without this, `test_default_is_off` would pass against a switch that blocks
    unconditionally — a guard that always denies is indistinguishable from a
    broken one.
    """
    _out, stage = stage_vault_injection(_req(), {"vault_injection": "json_inject"})

    assert stage.skip_reason != "disabled_by_config"


def test_switch_is_read_at_call_time_not_import_time(fake_retrieval, monkeypatch):
    """Flipping the switch takes effect without re-importing the pipeline.

    A value captured at import time cannot be changed at runtime, which would
    make the kill switch useless in exactly the incident it exists for.
    """
    monkeypatch.setattr(cfg_mod, "VAULT_INJECTION_ENABLED", True, raising=False)
    _out, on = stage_vault_injection(_req(), {"vault_injection": "json_inject"})

    monkeypatch.setattr(cfg_mod, "VAULT_INJECTION_ENABLED", False, raising=False)
    _out2, off = stage_vault_injection(_req(), {"vault_injection": "json_inject"})

    assert on.skip_reason != "disabled_by_config", "switch-on state was not honoured"
    assert off.skip_reason == "disabled_by_config", "switch-off did not take effect at call time"
