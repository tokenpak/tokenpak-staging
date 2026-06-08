"""TokenPak Dispatch — ContextProvider boundary (Standards Delta v0 §5.9).

OSS context assembly for station runs. :class:`LocalContextProvider` is the
deterministic, no-LLM/no-network OSS implementation; :class:`PaidContextProvider`
is a Pro-path stub (raises ``NotImplementedError``) that keeps the OSS/Pro
boundary (Std 25 §1.1 + §9.3) visible from day one. Phase D activation is a
constructor swap of the provider instance, not a rewrite.
"""

from __future__ import annotations

from .local import (
    DEFAULT_STATION_SIZE_BUDGET_BYTES,
    DEFAULT_STATION_TOKEN_BUDGET,
    GitignoreMatcher,
    LocalContextProvider,
    estimate_tokens,
)
from .provider import (
    ContextBundle,
    ContextFile,
    ContextProvider,
    ContextSource,
    PaidContextProvider,
    source_rank,
)

__all__ = [
    "ContextProvider",
    "ContextBundle",
    "ContextFile",
    "ContextSource",
    "source_rank",
    "PaidContextProvider",
    "LocalContextProvider",
    "GitignoreMatcher",
    "estimate_tokens",
    "DEFAULT_STATION_SIZE_BUDGET_BYTES",
    "DEFAULT_STATION_TOKEN_BUDGET",
]
