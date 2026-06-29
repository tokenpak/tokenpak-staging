"""Release-gate guard: the SDK Path example in ``docs/quickstart.md`` must run.

The quickstart's SDK Path is executable documentation — every ``>>>`` block is
run as a doctest against the real ``tokenpak.compression.pack`` API
(``ContextPack`` / ``PackBlock``). If the public packing API drifts (a method is
renamed, a signature changes, the example is deleted), this test fails the build
instead of shipping a quickstart that raises on the first line.

Equivalent to::

    pytest --doctest-glob='docs/quickstart.md' -q

but runs inside the normal ``pytest`` collection so plain CI catches drift.

Traces to: p0-adopt-sdk-quickstart-working-2026-06-16 (adoptability release-gate).
"""

from __future__ import annotations

import doctest
from pathlib import Path

import pytest

# tokenpak/tests/ -> tokenpak/ -> repo root; docs/ is not shipped in the wheel,
# so resolve it relative to the source checkout where CI runs.
QUICKSTART = Path(__file__).resolve().parents[2] / "docs" / "quickstart.md"


def test_quickstart_sdk_doctest_runs_clean() -> None:
    """Every ``>>>`` example in docs/quickstart.md executes without error."""
    if not QUICKSTART.is_file():
        pytest.skip(f"docs/quickstart.md absent (non-source install): {QUICKSTART}")

    failures, attempted = doctest.testfile(
        str(QUICKSTART),
        module_relative=False,
        optionflags=doctest.ELLIPSIS,
        verbose=False,
    )

    # Guard against the example being silently removed: a quickstart with no
    # runnable SDK example is itself the drift we are trying to catch.
    assert attempted > 0, (
        f"no doctest examples found in {QUICKSTART} — the SDK Path example is "
        "missing or no longer executable"
    )
    assert failures == 0, (
        f"{failures} doctest failure(s) in {QUICKSTART}: the quickstart SDK "
        "example has drifted from the real ContextPack/PackBlock API"
    )
