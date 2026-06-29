"""Disagreement scorer v0 (deliberation policy §7) — rule-based, deterministic, no judge call.

Inputs are normalized node outputs (§6). The scorer compares:

- **Verdict distance** — max pairwise ordinal distance on the
  ``approve < revise < escalate < stop`` ladder.
- **Reason-code / risk-flag overlap** — Jaccard similarity of the enum sets
  (intersection-of-all over union-of-all). Registry severity weighting for
  risk flags is Phase 0B (the minimum path does not load registry catalogues).
- **Confidence gap** — max ordinal spread of node ``confidence`` values.

It returns a :class:`~tokenpak.orchestration.deliberation.models.DisagreementResult`
with an explicit ``escalate`` flag. A judge pass runs only when the scorer
escalates or the mode requires it — never as an implicit default (§7).
"""

from __future__ import annotations

from itertools import combinations
from typing import Sequence

from pydantic import Field

from .models import (
    CONFIDENCE_ORDER,
    VERDICT_ORDER,
    DeliberationBaseModel,
    DisagreementResult,
    NodeOutput,
)


class ScorerThresholds(DeliberationBaseModel):
    """Configurable §7 thresholds. Defaults are provisional pending Phase 0B."""

    material_verdict_distance: int = Field(default=2, ge=1)
    minor_verdict_distance: int = Field(default=1, ge=1)
    reason_overlap_floor: float = Field(default=0.5, ge=0.0, le=1.0)
    confidence_gap_threshold: int = Field(default=2, ge=1)


def _jaccard(sets: list[frozenset[str]]) -> float:
    """Jaccard overlap across the nodes that assert codes/flags.

    Empty sets are excluded before comparing: an ``approve`` node legitimately
    carries no reason codes (§6), and counting its empty set would collapse
    the overlap to 0 against any node that does cite causes. Disagreement on
    *causes* is measured among the nodes that assert causes; fewer than two
    asserting nodes is vacuous agreement.
    """
    asserting = [s for s in sets if s]
    if len(asserting) < 2:
        return 1.0
    union = frozenset().union(*asserting)
    intersection = asserting[0]
    for s in asserting[1:]:
        intersection = intersection & s
    return len(intersection) / len(union)


def score(
    nodes: Sequence[NodeOutput],
    thresholds: ScorerThresholds | None = None,
) -> DisagreementResult:
    """Classify disagreement across node outputs (deterministic, §7 rules)."""
    t = thresholds or ScorerThresholds()

    if len(nodes) < 2:
        return DisagreementResult(classification="agree")

    verdict_distance = max(
        abs(VERDICT_ORDER[a.verdict] - VERDICT_ORDER[b.verdict])
        for a, b in combinations(nodes, 2)
    )
    reason_overlap = _jaccard([frozenset(n.reason_codes) for n in nodes])
    risk_overlap = _jaccard([frozenset(n.risk_flags) for n in nodes])
    confidence_gap = max(
        abs(CONFIDENCE_ORDER[a.confidence] - CONFIDENCE_ORDER[b.confidence])
        for a, b in combinations(nodes, 2)
    )

    if verdict_distance >= t.material_verdict_distance or (
        verdict_distance >= t.minor_verdict_distance
        and reason_overlap < t.reason_overlap_floor
    ):
        classification = "material-disagreement"
    elif (
        verdict_distance >= t.minor_verdict_distance
        or confidence_gap >= t.confidence_gap_threshold
        or reason_overlap < t.reason_overlap_floor
        or risk_overlap < t.reason_overlap_floor
    ):
        classification = "minor-divergence"
    else:
        classification = "agree"

    return DisagreementResult(
        classification=classification,
        max_verdict_distance=verdict_distance,
        reason_code_overlap=reason_overlap,
        risk_flag_overlap=risk_overlap,
        max_confidence_gap=confidence_gap,
        escalate=classification == "material-disagreement",
    )
