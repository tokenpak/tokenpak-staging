"""
tokenpak.orchestration.retry
─────────────────────────────
Compatibility re-export.

The retry primitive (5-level escalation: wait+retry, model downgrade,
provider switch, agent handoff, human alert) now lives in
:mod:`tokenpak.core.retry` — a lower layer that both ``routing`` and
``orchestration`` may import (see ``BURN-A4-G7`` in
``docs/import-debt-ledger.md``). This module re-exports the same public
names under the same import path so every existing
``from tokenpak.orchestration.retry import ...`` caller keeps working
unchanged.
"""

from __future__ import annotations

from tokenpak.core.retry import DEFAULT_PER_ERROR as DEFAULT_PER_ERROR
from tokenpak.core.retry import MODEL_DOWNGRADE_PATH as MODEL_DOWNGRADE_PATH
from tokenpak.core.retry import PROVIDER_FALLBACK_PATH as PROVIDER_FALLBACK_PATH
from tokenpak.core.retry import ImmediateAlertError as ImmediateAlertError
from tokenpak.core.retry import RetryAttempt as RetryAttempt
from tokenpak.core.retry import RetryEngine as RetryEngine
from tokenpak.core.retry import RetryExhaustedError as RetryExhaustedError

# Private helper, not part of the public API, re-exported only because
# existing tests import it directly by name.
from tokenpak.core.retry import _extract_http_status as _extract_http_status  # noqa: F401
from tokenpak.core.retry import load_recent_retry_events as load_recent_retry_events

__all__ = (
    "DEFAULT_PER_ERROR",
    "ImmediateAlertError",
    "MODEL_DOWNGRADE_PATH",
    "PROVIDER_FALLBACK_PATH",
    "RetryAttempt",
    "RetryEngine",
    "RetryExhaustedError",
    "load_recent_retry_events",
)
