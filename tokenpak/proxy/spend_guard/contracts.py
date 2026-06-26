# SPDX-License-Identifier: Apache-2.0
"""Spend Guard contracts (dataclasses) — the typed surface of the subsystem.

Kept minimal and free of internal dependencies so estimator/policy can run in
contexts that don't import the full proxy stack (e.g. unit tests, the
``[TIP: estimate=on]`` dry-run path, or future MCP/Pro consumers).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

# ---------------------------------------------------------------------------
# Estimator output
# ---------------------------------------------------------------------------

@dataclass
class RiskEstimate:
    """Projection of a single inbound request's cost.

    All token figures are *projected* totals for the upcoming provider call.
    ``current_context_tokens`` represents the running context the model will
    have after this request lands (cached + new).
    """

    model: str
    current_context_tokens: int
    request_tokens: int                 # uncached new content in this request
    projected_input_tokens: int         # current_context_tokens + request_tokens
    projected_output_tokens: int        # heuristic: max_tokens hint or default
    projected_cost_usd: float           # full input+output cost at model rates
    cache_hit_ratio: float              # 0.0..1.0 — fraction we expect cached
    rates: dict = field(default_factory=dict)  # {input, output, cached} per MTok

    # ── Receipt-ready preflight extensions (additive; default-empty) ──
    # Ranked context contributors — what is filling the model's context for
    # this request, biggest first. Each entry is a plain JSON-serializable
    # dict ``{"source": str, "tokens": int, "pct": float}`` where ``pct`` is
    # the share of ``projected_input_tokens``. Plain dicts (not a nested
    # dataclass) keep the public API surface stable and receipts cheap to
    # serialize.
    contributors: list = field(default_factory=list)
    # When ``contributors`` is empty, the reason it could not be computed —
    # e.g. ``"unparseable_request_body"`` or ``"non_messages_request_shape"``.
    # ``None`` once contributors are populated.
    contributors_reason: Optional[str] = None
    # True when the estimator could not parse the request body, so the token/
    # cost figures are a crude whole-body fallback (or zero). The policy turns
    # an unknown estimate into a ``warn`` rather than a silent ``allow`` — we
    # never record a confident verdict for a request we couldn't measure.
    estimate_unknown: bool = False
    # Machine token describing why the estimate is unknown (e.g.
    # ``"unparseable_request_body"``). ``None`` when the estimate is known.
    unknown_reason: Optional[str] = None


# ---------------------------------------------------------------------------
# Policy output
# ---------------------------------------------------------------------------

DecisionKind = Literal["allow", "warn", "block", "hard_block", "estimate_only", "cancel"]

# Schema marker stamped on receipt proofs so a downstream RequestReceiptV1 can
# version-gate the projection. Module-private (``_`` prefix) on purpose: it is
# an internal contract detail, not part of the package's public API surface.
_PROOF_VERSION = "preflight.v1"


@dataclass
class PreflightDecision:
    """Policy verdict on a RiskEstimate."""

    decision: DecisionKind
    reason: str                         # short machine token, e.g. ``projected_tokens_exceeded``
    requires_approval: bool             # True when caller can unblock with yes/[TIP]
    threshold_hit: Optional[str] = None  # the named threshold, for logging
    risk: Optional[RiskEstimate] = None

    def to_receipt_proof(self) -> dict:
        """Project this verdict into a Receipt-v1-ready proof object.

        Pure, JSON-serializable, no I/O — the spend/context preflight proof a
        downstream receipt (``RequestReceiptV1``) attaches verbatim. Captures
        the allow/warn/block/hard_block decision, the risk projection (or an
        explicit ``available: false`` marker with a reason when the estimate
        was unknown/absent), and the ranked context contributors (or the
        reason they're unavailable). Never raises.
        """
        risk = self.risk
        if risk is None:
            risk_block: dict = {"available": False, "reason": "no_estimate"}
            contributors: list = []
            contributors_reason: Optional[str] = "no_estimate"
        elif getattr(risk, "estimate_unknown", False):
            reason = getattr(risk, "unknown_reason", None) or "estimate_unavailable"
            risk_block = {
                "available": False,
                "reason": reason,
                "model": risk.model,
            }
            contributors = list(getattr(risk, "contributors", None) or [])
            contributors_reason = getattr(risk, "contributors_reason", None) or reason
        else:
            risk_block = {
                "available": True,
                "model": risk.model,
                "current_context_tokens": risk.current_context_tokens,
                "request_tokens": risk.request_tokens,
                "projected_input_tokens": risk.projected_input_tokens,
                "projected_output_tokens": risk.projected_output_tokens,
                "projected_cost_usd": risk.projected_cost_usd,
                "cache_hit_ratio": risk.cache_hit_ratio,
            }
            contributors = list(getattr(risk, "contributors", None) or [])
            contributors_reason = getattr(risk, "contributors_reason", None)
        return {
            "proof_version": _PROOF_VERSION,
            "decision": self.decision,
            "reason": self.reason,
            "requires_approval": self.requires_approval,
            "threshold_hit": self.threshold_hit,
            "risk": risk_block,
            "top_context_contributors": contributors,
            "contributors_reason": contributors_reason,
        }


# ---------------------------------------------------------------------------
# Pending store
# ---------------------------------------------------------------------------

@dataclass
class PendingRequest:
    """A request held back from the provider, awaiting approval."""

    pending_id: str
    session_id: str
    created_at: float
    expires_at: float
    request_hash: str
    provider: str
    model: str
    projected_tokens: int
    projected_cost_usd: float
    raw_request_blob: bytes             # gzipped original body — replay verbatim
    raw_request_headers: dict           # forwarded as-is on replay (auth etc.)
    target_url: str                     # provider URL to replay to
    status: Literal["pending", "consumed", "discarded", "expired"] = "pending"


# ---------------------------------------------------------------------------
# TIP directive
# ---------------------------------------------------------------------------

@dataclass
class TIPDirective:
    """Parsed ``[TIP: ...]`` control directive.

    All fields default-empty; presence indicates the directive set them. The
    raw text is preserved for audit.
    """

    raw: str = ""
    allow_scope: Optional[Literal["once", "15m", "session"]] = None
    # [TIP: allow=<N>] — pre-approve the next N blocked sends (count grant).
    # Mutually exclusive with allow_scope in the grammar; a positive int only.
    allow_count: Optional[int] = None
    bypass: bool = False
    max_cost_usd: Optional[float] = None
    max_tokens: Optional[int] = None
    ttl_seconds: Optional[int] = None  # [TIP: allow=session ttl=<sec>] grant window
    estimate_only: bool = False
    cancel: bool = False
    reason: Optional[str] = None
    # [TIP: deterministic=on] — reproducible eval mode (governed by the TIP
    # versioning standard). Disables output-changing proxy behaviors (upstream
    # retries, semantic response substitution, prompt mutation) and emits
    # reproducibility metadata. NOT a spend bypass: policy bands fire
    # exactly as without the directive.
    deterministic: bool = False
    # Fail-loud marker: an unsupported value (e.g. ``deterministic=maybe``)
    # is recorded here so the caller can REJECT the request with a
    # structured error. Per the reproducible-eval contract, unsupported
    # deterministic fields fail loudly — they are never silently stripped.
    deterministic_invalid_value: Optional[str] = None
    unknown_keys: list = field(default_factory=list)  # for warning audit


# ---------------------------------------------------------------------------
# Guard outcome — what the orchestrator returns to proxy/server.py
# ---------------------------------------------------------------------------

OutcomeKind = Literal[
    "forward",          # forward_body to provider unchanged
    "forward_modified", # forward_body with TIP-stripped bytes
    "block",            # return block_response_body to client; no provider call
    "hard_block",       # like block but explicitly cannot be bypassed
    "replay",           # forward_body is the consumed pending blob
    "estimate",         # return estimate_response_body to client
    "cancel",           # return cancel_response_body to client; pending discarded
    "reprompt",         # return reprompt_response_body to client; pending kept
]


@dataclass
class GuardOutcome:
    """Tagged result returned by ``evaluate``.

    The proxy hook reads ``kind`` and acts accordingly:
    - ``forward`` / ``forward_modified`` / ``replay`` → write ``body`` upstream
    - ``block`` / ``hard_block`` / ``estimate`` / ``cancel`` / ``reprompt`` →
      write ``response_body`` to the client with ``http_status``
    """

    kind: OutcomeKind
    body: Optional[bytes] = None              # bytes to forward upstream (forward/replay)
    headers: Optional[dict] = None            # headers to forward (replay only — original)
    target_url: Optional[str] = None          # provider URL for replay
    response_body: Optional[bytes] = None     # JSON to return to client now
    http_status: int = 200
    decision: Optional[PreflightDecision] = None
    pending_id: Optional[str] = None
    audit_event: Optional[str] = None         # event_type for audit row
    # Active budget-reservation hold for this forward (Standard 29 §15). The
    # proxy response path settles it via reservation.settle_reservation();
    # unsettled holds expire at their TTL.
    reservation_id: Optional[str] = None

    @classmethod
    def passthrough(cls, body: bytes) -> "GuardOutcome":
        """Default no-op outcome — guard disabled or estimator allowed."""
        return cls(kind="forward", body=body)
