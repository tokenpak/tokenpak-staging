# SPDX-License-Identifier: Apache-2.0
"""Forward-looking conformance guard for the curated CLI output contract.

These tests pin the v2 DX scriptability contract (audit B1/B2/B3, D8-C) so it
cannot silently regress and so a *newly registered* summary command cannot ship
without a JSON/quiet decision:

* Every curated summary verb still accepts a local ``--json`` flag.
* The Visibility summary surface is fully partitioned into json-required vs
  documented-exempt — adding a new Visibility command fails this test until its
  output contract is classified.
* The global ``--json`` / ``--quiet`` prefix is recognised, and ``--quiet`` does
  not alter exit-code semantics.
* The shared :func:`emit` pattern honours the json/quiet branches.

Per-command JSON *output* behaviour (stdout purity, no-data states) is covered
by ``test_cli_contract_gate.py`` and ``test_cost_savings_json_contract.py``;
this module guards the surface-level contract those tests assume.
"""

from __future__ import annotations

import io
import json

import pytest

from tokenpak import _cli_core
from tokenpak.cli import _output_contract as oc


@pytest.fixture(scope="module")
def parser():
    return _cli_core.build_parser()


@pytest.mark.parametrize("verb", oc.JSON_REQUIRED)
def test_json_required_commands_accept_json(parser, verb):
    """Each curated summary verb exposes a stable local ``--json`` flag."""
    assert oc.command_accepts_json(parser, verb), (
        f"summary command '{verb}' must accept --json (scriptability contract)"
    )


def test_visibility_surface_is_fully_classified():
    """Every Visibility verb is either json-required or documented-exempt.

    This is the forward-looking guard: a new summary command added to the
    registry's Visibility category fails here until someone classifies its
    output contract, which is exactly the drift the contract must catch.
    """
    visibility = set(oc.registry_commands_in_category(oc.SUMMARY_CATEGORY))
    required = set(oc.JSON_REQUIRED) & visibility
    exempt = set(oc.JSON_EXEMPT_VISIBILITY)

    unclassified = visibility - (required | exempt)
    assert not unclassified, (
        "new Visibility summary command(s) lack an output-contract decision: "
        f"{sorted(unclassified)} — add to JSON_REQUIRED or JSON_EXEMPT_VISIBILITY"
    )
    assert not (required & exempt), (
        f"command classified as both required and exempt: {sorted(required & exempt)}"
    )


def test_exempt_visibility_commands_exist_in_registry():
    """Exemptions must name real registry commands, not stale entries."""
    visibility = set(oc.registry_commands_in_category(oc.SUMMARY_CATEGORY))
    stale = set(oc.JSON_EXEMPT_VISIBILITY) - visibility
    assert not stale, f"exemptions reference non-Visibility commands: {sorted(stale)}"


@pytest.mark.parametrize("flag,key", [("--json", "json"), ("--quiet", "quiet"), ("-q", "quiet")])
def test_global_prefix_flags_recognised(flag, key):
    """``--json``/``--quiet``/``-q`` are consumed as a global prefix, leaving the verb."""
    remaining, opts = _cli_core._consume_global_prefix([flag, "status"])
    assert opts.get(key) is True
    assert remaining == ["status"], "global prefix must not consume the subcommand"


def test_quiet_does_not_change_unknown_flag_exit_code(parser):
    """``--quiet`` must not mask a usage error or change its exit code (2)."""
    with pytest.raises(SystemExit) as plain:
        parser.parse_args(["status", "--definitely-not-a-flag"])
    assert plain.value.code == 2

    # Quiet is a global prefix; the trailing usage error still exits 2.
    remaining, opts = _cli_core._consume_global_prefix(["--quiet", "status", "--definitely-not-a-flag"])
    assert opts.get("quiet") is True
    with pytest.raises(SystemExit) as quiet:
        parser.parse_args(remaining)
    assert quiet.value.code == 2


def test_emit_json_mode_emits_single_document():
    buf = io.StringIO()
    rc = oc.emit({"b": 2, "a": 1}, as_json=True, render=lambda _p: "CHROME", file=buf)
    assert rc == 0
    out = buf.getvalue()
    assert out.count("\n") == 1, "JSON mode emits exactly one document/newline"
    assert json.loads(out) == {"a": 1, "b": 2}
    assert "CHROME" not in out, "JSON mode must not include presentation chrome"


def test_emit_quiet_suppresses_chrome_but_allows_minimal_line():
    chrome_calls = []

    def render(_p):
        chrome_calls.append(1)
        return "FULL CHROME"

    buf = io.StringIO()
    oc.emit({"a": 1}, as_json=False, quiet=True, render=render, file=buf)
    assert chrome_calls == [], "quiet must not invoke the full chrome renderer"
    assert buf.getvalue() == ""

    buf = io.StringIO()
    oc.emit({"a": 1}, as_json=False, quiet=True, render=render, render_quiet=lambda _p: "ok", file=buf)
    assert buf.getvalue() == "ok\n"


def test_emit_normal_mode_renders_chrome():
    buf = io.StringIO()
    oc.emit({"a": 1}, as_json=False, quiet=False, render=lambda _p: "human view", file=buf)
    assert buf.getvalue() == "human view\n"


def test_emit_propagates_renderer_errors():
    """emit never swallows a renderer error — exit/error semantics stay with caller."""

    def boom(_p):
        raise RuntimeError("render failed")

    with pytest.raises(RuntimeError, match="render failed"):
        oc.emit({"a": 1}, as_json=False, quiet=False, render=boom, file=io.StringIO())
