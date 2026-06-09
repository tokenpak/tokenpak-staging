# SPDX-License-Identifier: Apache-2.0
"""Glue layer that turns guard primitives into a single ``GuardOutcome``.

The proxy hook (``proxy/server.py``) calls :func:`evaluate` with the raw
inbound bytes and gets back a tagged outcome. All multi-step interaction
(estimate → policy → pending → intent → replay → audit) lives here so the
proxy hot path stays small.

This module is layered: the core wires estimate + policy + pending; optional
layers add intent parsing, TIP-header handling, and audit logging. Each
optional layer is imported lazily so the core runs whether or not it is present.
"""

from __future__ import annotations

import logging
from typing import Optional

from .block_response import (
    block as build_block,
)
from .block_response import (
    cancelled as build_cancelled,
)
from .block_response import (
    estimate_only as build_estimate,
)
from .block_response import (
    hard_block as build_hard_block,
)
from .block_response import (
    pending_waiting as build_pending_waiting,
)
from .block_response import (
    reprompt as build_reprompt,
)
from .contracts import GuardOutcome, PendingRequest
from .estimator import estimate as run_estimate
from .pending import PendingStore, hash_request
from .policy import SpendGuardConfig, decide, load_config

_log = logging.getLogger(__name__)


# Provider hostnames keyed off the target_url for audit/store metadata.
def _provider_from_url(url: str) -> str:
    if not url:
        return ""
    if "anthropic" in url:
        return "anthropic"
    if "openai" in url or "codex" in url:
        return "openai"
    if "googleapis" in url or "vertex" in url:
        return "google"
    return "unknown"


def evaluate(
    body: bytes,
    model: str,
    session_id: str,
    headers: dict,
    *,
    config: Optional[SpendGuardConfig] = None,
    target_url: str = "",
) -> GuardOutcome:
    """Run the full pre-send pipeline.

    Returns a :class:`GuardOutcome` the proxy hook will interpret. Fails
    open (returns ``forward``) on any internal error.
    """
    cfg = config or load_config()

    # Disabled → forward unchanged. This is the soft-launch path.
    if not cfg.enabled:
        return GuardOutcome.passthrough(body)

    # TIP-header layer: parse + strip. Imported lazily so the core runs even
    # before this layer is present. Until tip_header.py exists, treat as no-op.
    tip_directive = None
    forward_body = body
    try:
        from .tip_header import parse_and_strip_tip_header
        tip_directive, forward_body = parse_and_strip_tip_header(body)
    except ImportError:
        pass
    except Exception as e:
        _log.debug("spend_guard: TIP header parse failed: %s", e)

    # Composite Yes-grant key (Standard 29 §"Yes-grant scope", W1):
    # (session_id, fleet_id, principal/agent_id). Extract the principal
    # dimensions once so every grant path (create/redeem/discard) keys
    # identically. Case-insensitive header lookup; lowercased for stable keys.
    agent_id = _header(headers, "x-tokenpak-agent").lower()
    fleet_id = _header(headers, "x-tokenpak-fleet").lower()

    # W5 visibility: when Yes-grants are configured to cover rolling caps,
    # log on every request so the weakened fleet protection stays auditable.
    if cfg.yes_grant_covers_rolling_caps:
        _log.warning(
            "spend_guard: yes_grant_covers_rolling_caps=True — Yes-grants are "
            "bypassing rolling caps; fleet-level spend protection is weakened "
            "(Standard 29 §'Yes-grant scope', W5)."
        )

    # TIP cancel — discard any pending (and any active grant) and acknowledge.
    if tip_directive is not None and tip_directive.cancel:
        _discard_grant(cfg, session_id, fleet_id, agent_id)
        store = PendingStore(cfg.audit_db_path)
        existing = store.get_by_session(session_id)
        if existing:
            store.discard(existing.pending_id)
            _audit(cfg, "cancel", session_id, decision_str="cancel",
                   pending_id=existing.pending_id, tip=tip_directive)
            return GuardOutcome(
                kind="cancel",
                response_body=build_cancelled(existing),
                http_status=200,
                pending_id=existing.pending_id,
                audit_event="cancel",
            )
        # Nothing to cancel — treat as no-op forward (with TIP stripped).
        return GuardOutcome(kind="forward_modified", body=forward_body)

    # Intent layer: pending check + intent parse. Lazy import for the same
    # reason — the core can run before the intent parser is present.
    store = PendingStore(cfg.audit_db_path)
    existing_pending = store.get_by_session(session_id)
    if existing_pending is not None:
        try:
            from .intent import Intent, parse_intent
            from .replay import resolve_pending
            intent = parse_intent(forward_body)
            # allow=N / bare-integer reply ("20"): a positive count is itself an
            # approval — treated as POSITIVE so the held request replays — that
            # also pre-approves the next N-1 blocked sends via a count grant.
            count = _approval_count(forward_body, tip_directive)
            effective_intent = intent
            if count is not None and intent != Intent.NEGATIVE:
                effective_intent = Intent.POSITIVE
            outcome = resolve_pending(
                store=store,
                pending=existing_pending,
                intent=effective_intent,
                tip=tip_directive,
                cfg=cfg,
                builders={
                    "cancelled": build_cancelled,
                    "reprompt": build_reprompt,
                    "pending_waiting": build_pending_waiting,
                },
            )
            _audit(cfg, outcome.audit_event or "pending", session_id,
                   decision_str=outcome.kind, pending_id=existing_pending.pending_id,
                   tip=tip_directive)
            # Yes-grant lifecycle on the approval turn. A turn-scoped Yes
            # (POSITIVE intent or [TIP: allow=session]) opens a grant so the
            # rest of the turn skips the soft-block prompt; allow=N opens a
            # count-bounded grant; an explicit allow=once / bare bypass keeps
            # single-request semantics. A NEGATIVE/cancel tears any grant down.
            if outcome.kind == "replay":
                if count is not None:
                    _create_grant(cfg, session_id, fleet_id, agent_id,
                                  tip_directive, existing_pending.pending_id,
                                  count=count)
                elif _should_create_grant(intent, tip_directive):
                    _create_grant(cfg, session_id, fleet_id, agent_id,
                                  tip_directive, existing_pending.pending_id)
            elif outcome.kind == "cancel":
                _discard_grant(cfg, session_id, fleet_id, agent_id)
            return outcome
        except ImportError:
            # Fallback before the intent layer: subsequent requests during a pending
            # block are themselves blocked with a "waiting approval" message.
            _audit(cfg, "pending_waiting", session_id,
                   decision_str="block", pending_id=existing_pending.pending_id,
                   tip=tip_directive)
            return GuardOutcome(
                kind="block",
                response_body=build_pending_waiting(existing_pending),
                http_status=402,
                pending_id=existing_pending.pending_id,
                audit_event="pending_waiting",
            )

    # No pending for this session. If a Yes-grant is active and this request
    # carries a NEGATIVE/cancel signal, tear the grant down before it can be
    # redeemed (design §Cancel; acceptance (d)).
    _maybe_discard_grant_on_negative(
        cfg, session_id, fleet_id, agent_id, forward_body, tip_directive
    )

    # ── Anti-loop: if the same request_hash was blocked very recently,
    #    return the cached block without re-running the estimator.
    h = hash_request(forward_body, model)
    recent = store.recent_block_by_hash(h, within_seconds=30.0)
    if recent is not None and recent.status in ("pending", "expired", "discarded"):
        _audit(cfg, "anti_loop_hit", session_id,
               decision_str="block", pending_id=recent.pending_id,
               tip=tip_directive)
        return GuardOutcome(
            kind="block",
            response_body=build_block(_synthetic_decision(recent), recent),
            http_status=402,
            pending_id=recent.pending_id,
            audit_event="anti_loop_hit",
        )

    # ── Estimate + decide
    try:
        est = run_estimate(forward_body, model)
    except Exception as e:
        _log.warning("spend_guard: estimator failure (passthrough): %s", e)
        return GuardOutcome.passthrough(body)

    # ── Rolling/cumulative caps (2026-05-15 post-incident P0).
    # Records the session→agent mapping for future per-agent lookups,
    # then evaluates per-agent and per-fleet rolling caps. If any cap
    # would be exceeded by this request's projected cost, return a
    # block (respect TIP bypass directives). Per-session caps continue
    # to evaluate downstream — rolling caps SUPPLEMENT them.
    try:
        from .block_response import build_rolling_cap_block
        from .rolling_caps import (
            RollingCapsConfig,
            check_rolling_caps,
            record_session_agent,
        )
        # Agent attribution — case-insensitive header lookup.
        agent_id = ""
        for hk, hv in (headers or {}).items():
            if str(hk).lower() == "x-tokenpak-agent":
                agent_id = str(hv).strip().lower()
                break
        if agent_id and session_id:
            record_session_agent(session_id, agent_id)
        if cfg.rolling_caps_enabled and agent_id:
            rc_cfg = RollingCapsConfig(
                enabled=cfg.rolling_caps_enabled,
                window_seconds=cfg.rolling_caps_window_seconds,
                per_agent_max_cost_usd=cfg.rolling_caps_per_agent_max_cost_usd,
                per_agent_max_tokens_total=cfg.rolling_caps_per_agent_max_tokens_total,
                per_agent_max_cache_read_tokens=cfg.rolling_caps_per_agent_max_cache_read_tokens,
                per_fleet_max_cost_usd=cfg.rolling_caps_per_fleet_max_cost_usd,
                per_fleet_max_tokens_total=cfg.rolling_caps_per_fleet_max_tokens_total,
                per_fleet_max_cache_read_tokens=cfg.rolling_caps_per_fleet_max_cache_read_tokens,
            )
            # Estimator doesn't directly project cache_read; use ratio
            # from est.cache_hit_ratio applied to projected_input_tokens
            # as a conservative estimate.
            projected_cache_read = int(est.projected_input_tokens * float(getattr(est, "cache_hit_ratio", 0.0) or 0.0))
            breach = check_rolling_caps(
                agent_id=agent_id,
                projected_cost_usd=float(est.projected_cost_usd),
                projected_input_tokens=int(est.projected_input_tokens),
                projected_output_tokens=int(est.projected_output_tokens),
                projected_cache_read_tokens=projected_cache_read,
                config=rc_cfg,
            )
            if breach is not None:
                # TIP bypass respects existing semantics: [TIP: bypass=on]
                # or [TIP: allow=once] both let this request through.
                tip_allowed = (
                    tip_directive is not None and (
                        tip_directive.bypass or tip_directive.allow_scope is not None
                    )
                )
                if not tip_allowed:
                    # W5: a Yes-grant does NOT cover rolling caps by default.
                    # Only when yes_grant_covers_rolling_caps is explicitly on
                    # may an active grant bypass the cap.
                    grant_bypass = None
                    if cfg.yes_grant_covers_rolling_caps:
                        grant_bypass = _try_grant_redeem(
                            cfg, session_id, fleet_id, agent_id, est,
                            forward_body, body, tip_directive,
                        )
                    if grant_bypass is not None:
                        return grant_bypass
                    _audit(cfg, "rolling_cap_block", session_id,
                           decision_str="rolling_cap_block",
                           projected_cost=est.projected_cost_usd, tip=tip_directive)
                    return GuardOutcome(
                        kind="block",
                        response_body=build_rolling_cap_block(breach),
                        http_status=402,
                        audit_event="rolling_cap_block",
                    )
                else:
                    _audit(cfg, "rolling_cap_tip_bypass", session_id,
                           decision_str="allow",
                           projected_cost=est.projected_cost_usd, tip=tip_directive)
    except ImportError:
        # rolling_caps module not yet installed — skip silently
        pass
    except Exception as e:
        _log.debug("spend_guard: rolling-cap check failed (passthrough): %s", e)

    # Session-cumulative running cost — read from monitor.db.
    session_running = 0.0
    if cfg.session_block_cost_usd > 0:
        try:
            from .session_state import session_cumulative_cost
            session_running = session_cumulative_cost(
                session_id, window_seconds=cfg.session_window_seconds
            )
        except Exception as e:
            _log.debug("spend_guard: session_state lookup failed: %s", e)
            session_running = 0.0

    # Resolve max context for THIS model so decide() can derive the
    # block-tokens band as 80% of context. None → fallback path inside
    # decide() (uses cfg.block_tokens). Lookup is best-effort; any error
    # falls through to fallback.
    model_max_context_tokens: Optional[int] = None
    try:
        from ._context_window import get_model_max_context
        model_max_context_tokens = get_model_max_context(model)
    except Exception as e:
        _log.debug("spend_guard: context_window lookup failed: %s", e)

    decision = decide(
        est, cfg, tip=tip_directive,
        session_running_cost_usd=session_running,
        model_max_context_tokens=model_max_context_tokens,
    )

    # ── [TIP: estimate=on] short-circuit (only when allowed by policy)
    if tip_directive is not None and tip_directive.estimate_only and decision.decision != "hard_block":
        _audit(cfg, "estimate", session_id, decision_str="estimate", tip=tip_directive,
               projected_cost=est.projected_cost_usd)
        return GuardOutcome(
            kind="estimate",
            response_body=build_estimate(est),
            http_status=200,
            decision=decision,
            audit_event="estimate",
        )

    # ── Allow / warn → forward
    if decision.decision in ("allow", "warn"):
        if decision.decision == "warn":
            _audit(cfg, "warn", session_id, decision_str="warn",
                   projected_cost=est.projected_cost_usd, tip=tip_directive)
        # Even with TIP-bypass we audit (allow path with tip_directive set)
        if tip_directive is not None:
            _audit(cfg, "tip_bypass", session_id, decision_str="allow",
                   projected_cost=est.projected_cost_usd, tip=tip_directive)
        # Proactive count: a fresh (non-reply) request carrying [TIP: allow=N]
        # approves THIS send as #1 and pre-arms a count grant for the next N-1
        # blocked sends — same mental model as replying N to a 402, so the
        # "prepend [TIP: allow=N]" promise holds without a reply round-trip.
        # (Reached only after the hard-block and rolling-cap checks above, so
        # those bands stay non-bypassable; allow=1 opens no grant.)
        if tip_directive is not None and getattr(tip_directive, "allow_count", None):
            _create_grant(cfg, session_id, fleet_id, agent_id,
                          tip_directive, "", count=tip_directive.allow_count)
        kind = "forward_modified" if forward_body is not body else "forward"
        return GuardOutcome(kind=kind, body=forward_body, decision=decision)

    # ── Hard-block → return immediately, no pending stored
    if decision.decision == "hard_block":
        _audit(cfg, "hard_block", session_id, decision_str="hard_block",
               projected_cost=est.projected_cost_usd, tip=tip_directive)
        return GuardOutcome(
            kind="hard_block",
            response_body=build_hard_block(decision),
            http_status=402,
            decision=decision,
            audit_event="hard_block",
        )

    # ── Block → an active Yes-grant lets the rest of the turn through
    #    without re-prompting (the whole point of session-scoped grants).
    #    Redemption increments the grant's budget/TTL bookkeeping and audits
    #    per-redemption (W2); a read error fails closed to the block band (W3).
    if decision.decision == "block":
        redeemed = _try_grant_redeem(
            cfg, session_id, fleet_id, agent_id, est,
            forward_body, body, tip_directive,
        )
        if redeemed is not None:
            return redeemed

    # ── Block → store pending, return block JSON
    pending = store.store(
        session_id=session_id,
        body=body,                 # store original (pre-TIP-strip) bytes
        headers=headers,
        target_url=target_url,
        provider=_provider_from_url(target_url),
        model=model,
        projected_tokens=est.projected_input_tokens + est.projected_output_tokens,
        projected_cost_usd=est.projected_cost_usd,
        ttl_seconds=cfg.pending_ttl_seconds,
    )
    _audit(cfg, "block", session_id, decision_str="block",
           pending_id=pending.pending_id, projected_cost=est.projected_cost_usd,
           tip=tip_directive)
    return GuardOutcome(
        kind="block",
        response_body=build_block(decision, pending),
        http_status=402,
        decision=decision,
        pending_id=pending.pending_id,
        audit_event="block",
    )


def _synthetic_decision(recent: PendingRequest):
    """Reconstruct a minimal PreflightDecision for anti-loop block responses."""
    from .contracts import PreflightDecision, RiskEstimate

    risk = RiskEstimate(
        model=recent.model,
        current_context_tokens=0,
        request_tokens=0,
        projected_input_tokens=recent.projected_tokens,
        projected_output_tokens=0,
        projected_cost_usd=recent.projected_cost_usd,
        cache_hit_ratio=0.0,
        rates={},
    )
    return PreflightDecision(
        decision="block",
        reason="anti_loop_cache",
        requires_approval=True,
        threshold_hit="anti_loop",
        risk=risk,
    )


def _audit(cfg, event_type, session_id, **fields) -> None:
    """Best-effort audit-log write. Never raises into the hot path."""
    try:
        from .audit import write_audit
        write_audit(cfg.audit_db_path, event_type=event_type,
                    session_id=session_id, **fields)
    except ImportError:
        # audit layer not present
        pass
    except Exception as e:
        _log.debug("spend_guard: audit write failed: %s", e)


# ── Yes-grant helpers (Standard 29 §"Yes-grant scope") ─────────────────────
# All best-effort: a grant only removes the interactive Yes/No prompt, it is
# never a spend exemption. Every helper swallows its own errors so the guard
# hot path stays alive; the one place we care about read errors (redeem) audits
# and fails CLOSED to the block band (W3).


def _header(headers: dict, name: str) -> str:
    """Case-insensitive header lookup → stripped value (or "")."""
    if not headers:
        return ""
    target = name.lower()
    for k, v in headers.items():
        if str(k).lower() == target:
            return str(v).strip()
    return ""


def _should_create_grant(intent, tip) -> bool:
    """Decide whether the approval turn opens a turn-scoped grant.

    POSITIVE intent or ``[TIP: allow=session]`` → grant. An explicit
    ``[TIP: allow=once]`` (or a bare bypass) keeps single-request semantics
    and opens NO grant (preserves the prior single-request default).
    """
    if tip is not None:
        if tip.allow_scope == "session":
            return True
        if tip.allow_scope == "once":
            return False
    try:
        from .intent import Intent
        if intent == Intent.POSITIVE:
            return True
    except Exception:
        pass
    return False


def _approval_count(body, tip) -> Optional[int]:
    """The pre-approval count for this turn, or ``None``.

    Sourced from ``[TIP: allow=<N>]`` (``tip.allow_count``) or a bare-integer
    reply (``"20"``). Returns a positive int, or ``None`` when neither is
    present or the value is invalid (0 / negative / non-integer).
    """
    if tip is not None:
        n = getattr(tip, "allow_count", None)
        if isinstance(n, int) and n >= 1:
            return n
    try:
        from .intent import parse_count
        return parse_count(body)
    except Exception:
        return None


def _create_grant(cfg, session_id, fleet_id, agent_id, tip, pending_id, count=None) -> None:
    """Open a session-scoped Yes-grant for the composite key (W1).

    TTL defaults to ``cfg.yes_grant_ttl_seconds``; ``[TIP: ttl=<sec>]`` and
    ``[TIP: max=$<usd>]`` override the window / attach a dollar ceiling (W4).
    ``count`` (allow=N) attaches a request-count ceiling: the held request
    being approved is send #1, so the grant covers the remaining ``N-1`` blocked
    sends (allow=N == answering "yes" N times). ``count<=1`` opens no grant —
    a single approval, identical to ``allow=once``. Audits ``yes_grant_created``
    (W2). Best-effort — never raises.
    """
    try:
        from .grants import GrantStore
        ttl = cfg.yes_grant_ttl_seconds
        max_cost = None
        kind = "session"
        remaining_count = None
        if tip is not None:
            if tip.ttl_seconds:
                ttl = tip.ttl_seconds
            if tip.max_cost_usd is not None:
                max_cost = tip.max_cost_usd
            if tip.allow_scope == "session":
                kind = "tip_session"
        if count is not None:
            remaining_count = int(count) - 1
            kind = "count"
            if remaining_count < 1:
                # allow=1 (== a single yes / allow=once): no multi-request grant.
                return
        GrantStore(cfg.audit_db_path).create(
            session_id=session_id, fleet_id=fleet_id, agent_id=agent_id,
            ttl_seconds=ttl, granted_by_pending_id=pending_id or "",
            grant_kind=kind, max_cost_usd=max_cost, remaining_count=remaining_count,
        )
        _audit(cfg, "yes_grant_created", session_id,
               decision_str="allow", pending_id=pending_id, tip=tip)
    except Exception as e:
        _log.debug("spend_guard: grant create failed: %s", e)


def _discard_grant(cfg, session_id, fleet_id, agent_id) -> None:
    """Tear down any active grant for the composite key (NEGATIVE / cancel)."""
    try:
        from .grants import GrantStore
        if GrantStore(cfg.audit_db_path).discard(session_id, fleet_id, agent_id):
            _audit(cfg, "yes_grant_discarded", session_id, decision_str="cancel")
    except Exception as e:
        _log.debug("spend_guard: grant discard failed: %s", e)


def _maybe_discard_grant_on_negative(cfg, session_id, fleet_id, agent_id, body, tip) -> None:
    """With no pending in flight, a NEGATIVE intent or ``[TIP: cancel]`` still
    tears down an active grant before it can be redeemed (design §Cancel,
    acceptance (d))."""
    negative = False
    if tip is not None and tip.cancel:
        negative = True
    if not negative:
        try:
            from .intent import Intent, parse_intent
            if parse_intent(body) == Intent.NEGATIVE:
                negative = True
        except Exception:
            pass
    if negative:
        _discard_grant(cfg, session_id, fleet_id, agent_id)


def _try_grant_redeem(cfg, session_id, fleet_id, agent_id, est, forward_body, body, tip):
    """Redeem an active grant for this held request, if one exists.

    Returns a forward :class:`GuardOutcome` on ``REDEEMED`` (auditing
    ``yes_grant_bypass`` per redemption — W2). Returns ``None`` on
    ``NO_GRANT`` / ``EXPIRED`` / ``EXHAUSTED`` (auditing the latter two) so the
    caller falls through to the normal block band. On any grant-table read
    error, audits ``yes_grant_read_error`` and returns ``None`` — fail-closed
    to the block band (W3).
    """
    if not agent_id and not session_id:
        return None
    try:
        from .grants import EXHAUSTED, EXPIRED, REDEEMED, GrantStore
        status, _grant = GrantStore(cfg.audit_db_path).redeem(
            session_id, fleet_id, agent_id, float(est.projected_cost_usd),
        )
    except Exception as e:
        _log.debug("spend_guard: grant redeem failed (fail-closed): %s", e)
        _audit(cfg, "yes_grant_read_error", session_id, decision_str="block",
               projected_cost=est.projected_cost_usd)
        return None

    if status == REDEEMED:
        _audit(cfg, "yes_grant_bypass", session_id, decision_str="allow",
               projected_cost=est.projected_cost_usd, tip=tip)
        kind = "forward_modified" if forward_body is not body else "forward"
        return GuardOutcome(kind=kind, body=forward_body,
                            audit_event="yes_grant_bypass")
    if status == EXPIRED:
        _audit(cfg, "yes_grant_expired", session_id, decision_str="block",
               projected_cost=est.projected_cost_usd)
    elif status == EXHAUSTED:
        _audit(cfg, "yes_grant_exhausted", session_id, decision_str="block",
               projected_cost=est.projected_cost_usd)
    return None
