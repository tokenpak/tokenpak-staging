"""SSRM data contracts — Signals + Decision dataclasses.

Phase 1 keeps these dataclasses simple and JSON-serializable so the full
signal snapshot can be persisted in ``monitor.requests.ssrm_signals_json``
and ``ssrm_audit.ssrm_decisions.signals_json`` for postmortem replay.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

# Decision action enum (string literals so they're JSON-friendly).
ACTION_CONTINUE = "continue_current_session"
ACTION_WARN = "warn_user"
ACTION_BLOCK = "block_with_bypass"
ACTION_COMPRESS = "compress_or_recap"
ACTION_ROTATE = "start_new_session_with_recap"
ACTION_QUARANTINE = "stop_loss_quarantine"

ALL_ACTIONS = (
    ACTION_CONTINUE,
    ACTION_WARN,
    ACTION_BLOCK,
    ACTION_COMPRESS,
    ACTION_ROTATE,
    ACTION_QUARANTINE,
)

# Severity ordering for tie-breaks per parent §5 ("highest-severity action wins").
ACTION_SEVERITY = {
    ACTION_CONTINUE: 0,
    ACTION_WARN: 1,
    ACTION_COMPRESS: 2,
    ACTION_BLOCK: 3,
    ACTION_ROTATE: 4,
    ACTION_QUARANTINE: 5,
}


@dataclass
class Signals:
    """11 signals + a small set of derived/auxiliary fields.

    Field index notes match the parent SSRM build packet §3.
    """

    # Signal 1 — fresh-input-only context (kept for backwards-compat with
    # the OSS spend_guard's input-only view).
    context_pct_now: float = 0.0
    # Signal 2 — projected effective context % for the NEXT request given a
    # heuristic estimate of next user/agent turn size.
    context_pct_projected_next: float = 0.0
    # Signal 3 — rolling token burn rate per minute (sum of input + output
    # tokens over a 10-min window, divided by 10). Token-denominated.
    token_burn_rate_per_min: float = 0.0
    # Signal 4 — cache_read / input ratio across recent requests in this
    # session. >1 indicates accumulating cached prefix.
    cache_read_ratio: float = 0.0
    # Signal 5 — how many recent requests on this session share the same
    # canonical prompt hash.
    prompt_fingerprint_repeat_count: int = 0
    # Signal 6 — relevance heuristic 0-1, lower = drifted from session start.
    relevance_drift_score: float = 1.0
    # Signal 7 — task_id change detected vs. session-start task_id.
    task_id_change_detected: bool = False
    # Signal 8 — no-progress-guard ledger join for the agent owning this
    # session: 'progress' | 'neutral' | 'no_progress'.
    progress_signal: str = "neutral"
    # Signal 9 — count of requests on this session_id.
    session_age_turns: int = 0
    # Signal 10 — does this session have a saved recap (Pro Continuum)?
    # OSS Phase 1: always False.
    previous_recap_available: bool = False
    # Signal 11 — estimated token saving if rotating now. OSS Phase 1: 0.
    projected_rotation_savings: int = 0
    # Signal 12 (Amendment A1) — effective working context % the model
    # actually receives this request, including cache-read amplification.
    # Supersedes signal #1 in v3.1 decision rules.
    effective_context_pct: float = 0.0

    # Auxiliary observation fields (not strictly "signals" but useful
    # context attached to every audit row).
    effective_context_tokens: int = 0
    input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    model: str = ""
    model_max_context: int = 0
    session_id: str = ""
    agent_id: str = ""
    fingerprint: str = ""
    drift_algorithm: str = "jaccard_word_set"

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, default=str)


@dataclass
class Decision:
    """SSRM decision wrapping action + signals + reason.

    In Phase 1 ``advisory_only`` is always True and the proxy ignores the
    action for routing purposes. Decisions are still recorded so post-hoc
    analysis can validate that Phase 2 would have acted correctly.
    """

    action: str = ACTION_CONTINUE
    signals: Signals = field(default_factory=Signals)
    reason: str = "phase1-default-continue"
    advisory_only: bool = True

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "reason": self.reason,
            "advisory_only": self.advisory_only,
            "signals": asdict(self.signals),
        }
