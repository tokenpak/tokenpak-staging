"""Compatibility import path. The proxy launcher lives in
``tokenpak.proxy.bootstrap``; this module resolves the same names on first
access.

No name is imported eagerly: ``__getattr__`` below resolves
``tokenpak.proxy.bootstrap`` only when one of the names in ``__all__`` is
actually accessed, so importing this module alone never triggers a static
or eager import of the proxy layer.

The module is write-through as well as read-through: assigning or deleting
one of the ``__all__`` names on this module (e.g. ``shim.MONITOR = x``)
forwards to ``tokenpak.proxy.bootstrap`` instead of silently shadowing it
here, so code that still monkeypatches the old path (tests included) keeps
affecting the real runtime state. This is implemented below by swapping the
module's ``__class__`` to ``_CompatModule``, a ``types.ModuleType`` subclass
that overrides ``__setattr__``/``__delattr__``; see that class's docstring
for why ``__getattr__`` itself needs no equivalent change.
"""

from __future__ import annotations

import sys
import types
import warnings
from typing import Any

__all__ = [  # noqa: F822 -- names are resolved lazily by __getattr__ below
    "CLAUDE_CODE_HEADER_ALLOWLIST",
    "COMPILATION_MODE",
    "ForwardProxyHandler",
    "LEGACY_HEADER_ALLOWLIST",
    "MUTATION_AUDIT_TTL_DAYS",
    "Monitor",
    "SESSION",
    "STABLE_CACHE_CONTROL_AUTO",
    "TOKENPAK_HEADER_ALLOWLIST",
    "ThreadedHTTPServer",
    "can_compress",
    "extract_query_signal",
    "inject_vault_context",
]

_warned = False


def __getattr__(name: str) -> Any:
    global _warned
    if name in __all__:
        if not _warned:
            _warned = True
            warnings.warn(
                "tokenpak.core.runtime.proxy is a compatibility path; "
                "import from tokenpak.proxy.bootstrap",
                DeprecationWarning,
                stacklevel=2,
            )
        import importlib

        mod = importlib.import_module("tokenpak.proxy.bootstrap")
        return getattr(mod, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(__all__)


class _CompatModule(types.ModuleType):
    """``ModuleType`` subclass, installed onto this module's ``__class__``
    below, that forwards attribute *writes* and *deletes* for the names in
    ``__all__`` to ``tokenpak.proxy.bootstrap``, resolved lazily via
    ``importlib`` exactly as ``__getattr__`` above resolves reads. Names
    outside ``__all__`` are ordinary module attributes and set/delete on
    this module as usual.

    ``__getattr__`` needs no matching change: module attribute *reads* are
    dispatched by the interpreter straight to the ``__getattr__`` function
    living in this module's own namespace (PEP 562), a mechanism that does
    not go through ``__class__`` at all, so it already works unchanged
    regardless of this subclass. Writes and deletes have no PEP 562
    equivalent -- forwarding those requires overriding ``__setattr__`` /
    ``__delattr__`` at the type level, which is what this class -- and the
    ``__class__`` swap -- exist to do.
    """

    def __setattr__(self, name: str, value: Any) -> None:
        if name in __all__:
            import importlib

            bootstrap = importlib.import_module("tokenpak.proxy.bootstrap")
            setattr(bootstrap, name, value)
            return
        super().__setattr__(name, value)

    def __delattr__(self, name: str) -> None:
        if name in __all__:
            import importlib

            bootstrap = importlib.import_module("tokenpak.proxy.bootstrap")
            delattr(bootstrap, name)
            return
        super().__delattr__(name)


sys.modules[__name__].__class__ = _CompatModule
