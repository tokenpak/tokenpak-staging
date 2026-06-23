"""tests/test_dashboard_modes_deprecation_alias.py

F-08 (ultrareview L10c4): the public dashboard-modes constant was renamed off
its internal-ID-leaking name (`CCI09_DASHBOARD_MODES`) to a public-safe
`DASHBOARD_MODES`, with the old name retained as a deprecation alias per
Std 21 §11.2 (alias + api-snapshot ratchet, not a bare rename).

Covers:
  - new public-safe symbol is exposed and unchanged in value;
  - both names are recorded in `__all__` (deprecated name stays public);
  - accessing the deprecated alias emits exactly one DeprecationWarning and
    resolves to the canonical object;
  - accessing the canonical name emits no warning;
  - unknown attributes still raise AttributeError;
  - the on-disk public-API snapshot records both symbols (ratchet proof).
"""

from __future__ import annotations

import json
import warnings
from pathlib import Path

import pytest

import tokenpak.dashboard as dashboard

EXPECTED_MODES = ("cli", "tui", "tmux", "sdk", "ide", "cron")
DEPRECATED_NAME = "CCI09_DASHBOARD_MODES"
CANONICAL_NAME = "DASHBOARD_MODES"


def test_canonical_symbol_exposed_with_expected_value():
    assert dashboard.DASHBOARD_MODES == EXPECTED_MODES


def test_both_names_in_all():
    # Canonical name is exported; deprecated name remains exported so it is
    # still recorded as a (deprecated) public symbol until removal.
    assert CANONICAL_NAME in dashboard.__all__
    assert DEPRECATED_NAME in dashboard.__all__


def test_canonical_access_does_not_warn():
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        # Must not raise: canonical access is warning-free.
        assert dashboard.DASHBOARD_MODES == EXPECTED_MODES


def test_deprecated_alias_warns_and_resolves():
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        value = getattr(dashboard, DEPRECATED_NAME)

    deprecations = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert len(deprecations) == 1, f"expected exactly one DeprecationWarning, got {caught}"
    message = str(deprecations[0].message)
    assert DEPRECATED_NAME in message
    assert CANONICAL_NAME in message
    # Alias resolves to the canonical object (identity, not just equality).
    assert value is dashboard.DASHBOARD_MODES


def test_unknown_attribute_still_raises_attribute_error():
    with pytest.raises(AttributeError):
        _ = dashboard.DEFINITELY_NOT_A_REAL_ATTR


def test_snapshot_records_both_symbols():
    snapshot_path = Path(dashboard.__file__).resolve().parents[1] / "_snapshots" / "public-api.json"
    data = json.loads(snapshot_path.read_text())
    names = {
        (entry["module"], entry["name"])
        for entry in data.get("symbols", [])
    }
    assert ("tokenpak.dashboard", CANONICAL_NAME) in names
    assert ("tokenpak.dashboard", DEPRECATED_NAME) in names
