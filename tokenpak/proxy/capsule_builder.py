"""
tokenpak.proxy.capsule_builder (DEPRECATED)
============================================

.. deprecated:: 1.6.0
    Use ``tokenpak.proxy.pak_builder`` instead. Removal target: v2.0.0.
"""

from __future__ import annotations

import warnings as _warnings

_warnings.warn(
    "tokenpak.proxy.capsule_builder is deprecated; "
    "use tokenpak.proxy.pak_builder instead. Removal target: v2.0.0",
    DeprecationWarning,
    stacklevel=2,
)

from tokenpak.proxy.pak_builder import (  # noqa: E402, F401
    DEFAULT_HOT_WINDOW,
    DEFAULT_MIN_BLOCK_CHARS,
    PakBuilder as CapsuleBuilder,
    make_pak_builder as make_capsule_builder,
)

__all__ = [
    "CapsuleBuilder",
    "DEFAULT_HOT_WINDOW",
    "DEFAULT_MIN_BLOCK_CHARS",
    "make_capsule_builder",
]
