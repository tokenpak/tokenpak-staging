"""Compatibility guard for ``tokenpak.core.runtime.proxy``.

The proxy launcher relocated to ``tokenpak.proxy.bootstrap`` (its natural
proxy-layer home) to remove a forbidden core->proxy import edge. This module
now resolves the same 13 public names lazily via ``__getattr__``, so the old
import path keeps working while creating no static import edge into the
proxy layer.
"""

from __future__ import annotations

import subprocess
import sys

import pytest


def test_lazy_names_resolve_to_canonical_objects():
    from tokenpak.core.runtime import proxy as compat
    from tokenpak.proxy import bootstrap

    assert len(compat.__all__) == 13
    for name in compat.__all__:
        assert getattr(compat, name) is getattr(bootstrap, name)


def test_dir_contains_all_names():
    from tokenpak.core.runtime import proxy as compat

    listing = dir(compat)
    for name in compat.__all__:
        assert name in listing


def test_unknown_attribute_raises_attribute_error():
    from tokenpak.core.runtime import proxy as compat

    with pytest.raises(AttributeError):
        compat.not_a_real_symbol


def test_importing_compat_module_does_not_eagerly_import_bootstrap():
    """No static/eager edge: importing the compat module alone must not pull
    in tokenpak.proxy.bootstrap. Verified in a fresh subprocess so no other
    test's import caching can mask an eager edge."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import tokenpak.core.runtime.proxy, sys; "
            "print('tokenpak.proxy.bootstrap' in sys.modules)",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False", (
        f"importing the compat module eagerly pulled in bootstrap: {result.stdout!r} {result.stderr!r}"
    )


def test_first_access_emits_one_deprecation_warning():
    """Accessing a lazy name emits exactly one DeprecationWarning per process
    (module-level flag), verified in a fresh subprocess to isolate the
    once-per-process flag from other tests."""
    code = (
        "import warnings\n"
        "from tokenpak.core.runtime import proxy as compat\n"
        "with warnings.catch_warnings(record=True) as w:\n"
        "    warnings.simplefilter('always')\n"
        "    compat.SESSION\n"
        "    compat.COMPILATION_MODE\n"
        "    deprecation_warnings = [x for x in w if issubclass(x.category, DeprecationWarning)]\n"
        "    assert len(deprecation_warnings) == 1, deprecation_warnings\n"
        "print('OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "OK"
