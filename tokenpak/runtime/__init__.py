# SPDX-License-Identifier: Apache-2.0
"""Compatibility re-export — canonical source is tokenpak.core.runtime.

Runtime Hygiene exception: the registry-foundation modules (:mod:`.hygiene`,
:mod:`.hygiene_registry`, :mod:`.hygiene_schema`) are *new* foundation code
that lives here directly rather than as a ``tokenpak.core.runtime`` re-export
shim. They are imported below so ``tokenpak.runtime.hygiene`` is reliably
available, and they deliberately keep ``__all__ = []`` so they add no symbols
to the released public-API snapshot (internal plumbing, not a public claim).
"""
from tokenpak.core.runtime import *  # noqa: F401,F403

from . import hygiene, hygiene_registry, hygiene_schema  # noqa: F401
