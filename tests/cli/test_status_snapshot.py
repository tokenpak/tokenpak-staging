# SPDX-License-Identifier: Apache-2.0
"""Extraction-equivalence tests for the status provider shim.

The ``menu_status`` shim re-exports the proxy probe from
``tokenpak.status.snapshot`` with behavior preserved, and the new provider
package adds no public-API surface.
"""

from __future__ import annotations

from tokenpak.cli.commands import menu_status
from tokenpak.status import snapshot as provider


def test_menu_status_reexports_are_the_provider_objects():
    # Every symbol the menu/doctor/_cli_core/tests use must resolve to the
    # extracted provider object (identity), not a divergent copy.
    assert menu_status.ProxyStatus is provider.ProxyStatus
    assert menu_status.StatusCache is provider.StatusCache
    assert menu_status.snapshot is provider.snapshot
    assert menu_status.json_snapshot is provider.json_snapshot
    assert menu_status.reset_cache is provider.reset_cache
    assert menu_status._port is provider._port
    assert menu_status.STATUS_SCHEMA_VERSION == provider.STATUS_SCHEMA_VERSION


def test_menu_status_all_is_unchanged():
    # Frozen public-API snapshot records these verbatim — must not drift.
    assert menu_status.__all__ == [
        "ProxyStatus",
        "STATUS_SCHEMA_VERSION",
        "StatusCache",
        "reset_cache",
    ]


def test_provider_modules_add_no_public_surface():
    # __all__ == [] keeps the new package out of the frozen public-API snapshot.
    assert provider.__all__ == []
    import tokenpak.status as status_pkg

    assert status_pkg.__all__ == []


def test_json_snapshot_via_shim_is_stable_and_honest():
    menu_status.reset_cache()
    js = menu_status.json_snapshot()
    assert set(js) == {"schema_version", "proxy", "cost_today", "saved_today", "port"}
    assert js["schema_version"] == menu_status.STATUS_SCHEMA_VERSION
    assert js["proxy"] in {"running", "stopped", "starting", "unknown"}
    # fresh process, no forced probe ⇒ honest unknown, never fabricated
    assert js["cost_today"] is None
    assert js["saved_today"] is None


def test_snapshot_probe_false_is_deterministic_unknown():
    menu_status.reset_cache()
    s = menu_status.snapshot(probe=False)
    assert isinstance(s, menu_status.ProxyStatus)
    assert s.state == "unknown"
    assert s.cost is None and s.saved is None


def test_provider_exposes_render_agnostic_contract():
    # The new contract is importable from the package surface (by explicit path).
    from tokenpak.status import (
        RoutingMode,
        StatusField,
        StatusSnapshot,
        StatusSource,
        build_status_snapshot,
    )

    assert StatusSnapshot is provider.StatusSnapshot
    assert build_status_snapshot is provider.build_status_snapshot
    assert RoutingMode is provider.RoutingMode
    assert StatusSource is provider.StatusSource
    assert StatusField is provider.StatusField
