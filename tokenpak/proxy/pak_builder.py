"""
tokenpak.proxy.pak_builder
==========================

Proxy-layer access to the PakBuilder.

This module exposes :class:`PakBuilder` at the
``tokenpak.proxy.pak_builder`` import path that the proxy
pipeline expects, delegating to the canonical implementation in
``tokenpak.companion.capsules.builder``.

Typical use
-----------
::

    from tokenpak.proxy.pak_builder import PakBuilder

    builder = PakBuilder(enabled=True)
    new_body, stats = builder.process(request_body_bytes)

Or via the feature-flag-aware factory:

::

    from tokenpak.proxy.pak_builder import make_pak_builder

    builder = make_pak_builder()  # respects TOKENPAK_CAPSULE_BUILDER env var
    new_body, stats = builder.process(request_body_bytes)
"""

from __future__ import annotations

import os

from tokenpak.companion.capsules.builder import (
    DEFAULT_HOT_WINDOW,
    DEFAULT_MIN_BLOCK_CHARS,
    PakBuilder,
)

__all__ = [
    "PakBuilder",
    "DEFAULT_HOT_WINDOW",
    "DEFAULT_MIN_BLOCK_CHARS",
    "make_pak_builder",
]


def make_pak_builder(
    *,
    min_block_chars: int = DEFAULT_MIN_BLOCK_CHARS,
    hot_window: int = DEFAULT_HOT_WINDOW,
) -> PakBuilder:
    """
    Factory that reads the ``TOKENPAK_CAPSULE_BUILDER`` env var to decide
    whether the builder is enabled, then returns a ready-to-use
    :class:`PakBuilder`.

    Parameters
    ----------
    min_block_chars : int
        Minimum character length before a block is considered for
        compression (default: ``DEFAULT_MIN_BLOCK_CHARS``).
    hot_window : int
        Number of trailing messages to leave uncompressed
        (default: ``DEFAULT_HOT_WINDOW``).

    Returns
    -------
    PakBuilder
        An enabled or disabled builder depending on the feature flag.
    """
    enabled = os.environ.get("TOKENPAK_CAPSULE_BUILDER", "0") == "1"
    return PakBuilder(
        enabled=enabled,
        min_block_chars=min_block_chars,
        hot_window=hot_window,
    )
