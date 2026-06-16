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

__all__ = ["decide", "Signals", "Decision", "prewarm"]


def prewarm() -> None:
    """Idempotent best-effort startup prewarm for the SSRM subsystem.

    Eagerly resolves the config + creates the audit/state sqlite handles
    so the FIRST live request doesn't pay the lazy-init cost (which
    previously caused 1-of-N monitor rows to slip through with empty
    ssrm_* columns when SSRM is enabled).

    Strictly read/init only:
      - reads ~/.tokenpak/config.yaml (or its env-var overrides)
      - creates ssrm_audit.db + ssrm_state.db files if absent (no rows
        written; just CREATE TABLE IF NOT EXISTS)

    No-op when SSRM is disabled or when called multiple times. Never
    raises — any error is silently swallowed because the proxy must
    not fail to start because of an SSRM init hiccup.
    """
    try:
        from .policy import _get_ssrm_config
        cfg = _get_ssrm_config()
        if not cfg.get("enabled"):
            return
        # Touch both sqlite handles so first-request doesn't race.
        from .state import open_audit_db, open_state_db
        open_audit_db(cfg.get("audit_db_path", "~/.tokenpak/ssrm_audit.db"))
        open_state_db(cfg.get("state_db_path", "~/.tokenpak/ssrm_state.db"))
    except Exception:
        # Strict no-raise contract.
        pass
