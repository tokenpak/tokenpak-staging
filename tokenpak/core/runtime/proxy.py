"""Compatibility import path. The proxy launcher lives in
``tokenpak.proxy.bootstrap``; this module resolves the same names on first
access.

No name is imported eagerly: ``__getattr__`` below resolves
``tokenpak.proxy.bootstrap`` only when one of the names in ``__all__`` is
actually accessed, so importing this module alone never triggers a static
or eager import of the proxy layer.
"""

from __future__ import annotations

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
