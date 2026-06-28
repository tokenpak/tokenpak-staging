"""Deliberation Engine — internal layer of Deliberation Dispatch.

Minimum Engine path: emit a conformant Deliberation Receipt for a given
decision input from pre-collected normalized node outputs. No model calls.
Not to be confused with ``tokenpak.orchestration.dispatch`` (TokenPak
Dispatch / Run Ledger) — a ``DispatchReceipt`` is not a Deliberation Receipt.
"""

from .engine import (
    ENGINE_VERSION,
    DeliberationConfig,
    DeliberationEngine,
    DeliberationInput,
    DeliberationRecursionError,
)
from .models import (
    DeliberationReceipt,
    DisagreementResult,
    DissentRecord,
    FixedJudge,
    NodeOutput,
    PartialResult,
)
from .scorer import ScorerThresholds, score
