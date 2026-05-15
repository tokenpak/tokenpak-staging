"""SSRM — Session Swap Relevance Management (Phase 1, instrumentation-only).

Phase 1 computes 11 signals per request and records a recommended action
to ``ssrm_audit.db``, plus ssrm_* columns to ``monitor.db.requests``. The
proxy does NOT branch on the recommendation in Phase 1: every request
that would otherwise have been forwarded is still forwarded. The
recommendation is advisory only and is read later by post-hoc analysis
to decide whether Phase 2 should turn on behavior changes.

Public surface:

    from tokenpak.proxy.ssrm import decide, Signals, Decision

    decision = decide(
        body=request_body_bytes,
        model="claude-opus-4-7",
        session_id="...",
        headers={...},
    )
    # decision.action ∈ {
    #     "continue_current_session", "warn_user", "block_with_bypass",
    #     "compress_or_recap", "start_new_session_with_recap",
    #     "stop_loss_quarantine",
    # }
    # decision.signals is a Signals dataclass with all 11 inputs.
    # decision.advisory_only is True in Phase 1.
"""

from __future__ import annotations

from .contracts import Decision, Signals
from .policy import decide

__all__ = ["decide", "Signals", "Decision"]
