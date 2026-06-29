"""Help<->parser parity regression test for the CLI help catalog.

Guards the two honesty invariants behind the CLI help catalog:

1. **No phantom commands.** Every command advertised in the help registry
   (``commands.json``) is actually invokable by the parser. A user must never
   see a command in ``tokenpak help --all`` that returns ``Unknown command`` on
   invoke.
2. **No silent drift.** Every invokable-but-unadvertised command is a known,
   deliberate internal verb. A new invokable command that is neither cataloged
   in the registry nor recorded as a deliberate internal verb trips this test,
   forcing a conscious decision rather than silent re-drift.

Both checks derive the "invokable" set live from the argparse parser via
:func:`tokenpak._cli_core.registered_command_names`, so the advertised count can
never drift from what the CLI actually dispatches.
"""

import json
from pathlib import Path

from tokenpak._cli_core import build_parser, registered_command_names

REGISTRY_PATH = (
    Path(__file__).resolve().parent.parent
    / "tokenpak" / "core" / "registry" / "commands.json"
)

# Commands brought into the help catalog by the help-registry parity work:
# ``pak`` plus its verb family, and the previously-invisible-but-invokable
# commands that were cataloged so help advertises them.
HELP_CATALOG_ADDED = {
    "pak", "pakplan", "tip", "creds", "claude", "codex", "prove", "init", "setup",
}

# Phantom commands disposed by the same work (default = REMOVE; none confirmed
# against a supported catalog, none with near-term roadmap evidence). They must
# never reappear as advertised commands without becoming invokable.
HELP_CATALOG_DISPOSED = {
    "workflow", "handoff", "retain", "metrics",
    "policy", "sla", "compression", "maintenance",
}

# Known invokable-but-unadvertised verbs: commands that dispatch today but are
# deliberately kept OUT of the public help catalog (internal / dev /
# experimental verbs, and commands pending a separate cataloging decision).
# These are NOT phantoms -- they all dispatch.
#
# This allowlist is the count-free successor to the older single "expected
# count" snapshot, which had to be hand-recalibrated whenever the
# advertised/invokable surface differed between builds. The drift guard checks
# *membership*, not a number: a new invokable command that is neither
# advertised nor listed here trips ``test_no_silent_invisible_drift``. To
# resolve such a failure:
#   * if you added a user-facing command, catalog it in ``commands.json`` so
#     help advertises it; or
#   * if you added a deliberately-internal verb, add its name here in the same
#     change so the decision is explicit and reviewed.
# The check is directional -- every unadvertised verb must be known -- so a
# build that ships a strict subset of these verbs still passes without edits.
KNOWN_INTERNAL_UNADVERTISED = frozenset({
    "aggregate", "alerts", "attribution", "cards", "check-alerts",
    "companion", "compare", "config-check", "diagnose", "explain",
    "features", "fleet", "forecast", "help", "home",
    "integrate", "leaderboard", "learn", "menu", "monitor",
    "openclaw", "permissions", "preview", "recommendations", "requests",
    "retrieval", "telemetry", "test", "timeline", "uninstall",
    "usage", "validate-config", "vault-health", "watch",
})


def _registry_commands():
    data = json.loads(REGISTRY_PATH.read_text())
    return [c["command"] for c in data.get("commands", [])]


def _registry_entry(command: str):
    data = json.loads(REGISTRY_PATH.read_text())
    for entry in data.get("commands", []):
        if entry.get("command") == command:
            return entry
    raise AssertionError(f"command missing from registry: {command}")


def test_mission_verb_aliases_are_advertised_and_invokable():
    """Mission verbs are thin aliases, but still real user-facing CLI verbs."""
    invokable = registered_command_names(build_parser())
    advertised = set(_registry_commands())
    aliases = {"pack", "reuse", "guard", "receipt", "verify"}
    assert aliases <= advertised
    assert aliases <= invokable


def test_no_phantom_commands():
    """Every advertised command must be invokable (no ``Unknown command``)."""
    invokable = registered_command_names(build_parser())
    advertised = set(_registry_commands())
    phantoms = sorted(advertised - invokable)
    assert not phantoms, (
        "Help registry advertises command(s) the parser will not accept "
        f"(invoking them returns 'Unknown command'): {phantoms}"
    )


def test_added_commands_are_advertised_and_invokable():
    """The help-catalog additions surface in help AND dispatch."""
    invokable = registered_command_names(build_parser())
    advertised = set(_registry_commands())
    assert HELP_CATALOG_ADDED <= advertised, (
        f"missing from help registry: {sorted(HELP_CATALOG_ADDED - advertised)}"
    )
    assert HELP_CATALOG_ADDED <= invokable, (
        f"not invokable by parser: {sorted(HELP_CATALOG_ADDED - invokable)}"
    )


def test_disposed_phantoms_absent():
    """The disposed phantoms must not reappear in the registry."""
    advertised = set(_registry_commands())
    leaked = sorted(advertised & HELP_CATALOG_DISPOSED)
    assert not leaked, f"disposed phantom command(s) back in registry: {leaked}"


def test_no_silent_invisible_drift():
    """Every invokable-but-unadvertised verb must be a known internal verb.

    This is the count-free successor to the old "expected count" snapshot: it
    asserts *membership*, not a number, so it holds on any build whose
    unadvertised verbs are a subset of the known-internal allowlist without
    per-surface recalibration. A new invokable command that is neither
    advertised nor allowlisted trips the guard.
    """
    invokable = registered_command_names(build_parser())
    advertised = set(_registry_commands())
    unadvertised = invokable - advertised
    unexpected = sorted(unadvertised - KNOWN_INTERNAL_UNADVERTISED)
    assert not unexpected, (
        "Invokable command(s) are neither advertised in commands.json nor "
        f"recorded as deliberate internal verbs: {unexpected}. "
        "If a command is user-facing, add it to commands.json so help "
        "advertises it; if it is deliberately internal, add it to "
        "KNOWN_INTERNAL_UNADVERTISED in the same change so the decision is "
        "explicit and reviewed."
    )


def test_advertised_count_is_derived_not_hardcoded():
    """The count help advertises is derived from the live registry, and honest.

    ``help`` renders ``len(_load_registry())``; this asserts that derivation
    matches the registry file and -- combined with ``test_no_phantom_commands``
    -- that every counted command is genuinely invokable.
    """
    from tokenpak.cli.commands.help import _load_registry

    assert len(_load_registry()) == len(_registry_commands())


def test_activate_registry_copy_matches_fail_closed_activation():
    """Activation help must not promise immediate Pro unlock."""
    detail = _registry_entry("activate")["detail"]

    assert "pending_validation" in detail
    assert "Pro daemon verifies" in detail
    assert "not immediately" in detail
    assert "Enables features for your tier immediately" not in detail
