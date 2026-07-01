# SPDX-License-Identifier: Apache-2.0
"""Deterministic OSS Pak ranking for local recall.

This module intentionally stays small and transparent. It ranks user-controlled
local Paks from the OSS recall store using metadata that is already visible in
the open storage foundation: Pak type, authority, title/summary text,
reason-code weights, and risk flags.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

REASON_CODE_MULTIPLIERS: dict[str, float] = {
    "current_task": 2.0,
    "authoritative_decision": 1.8,
    "recent_user_reference": 1.4,
    "stable_context": 0.9,
    "project_relevant": 0.8,
    "ambient_context": 0.1,
}

PAK_TYPE_WEIGHTS: dict[str, float] = {
    "decision": 1.25,
    "interaction": 0.9,
    "session": 0.9,
    "vault": 0.65,
    "imported": 0.55,
}

AUTHORITY_WEIGHTS: dict[str, float] = {
    "user_approved": 1.25,
    "file_source": 0.9,
    "tool_result": 0.75,
    "imported": 0.65,
    "llm_generated": 0.45,
}

RISK_SEVERITY_PENALTIES: dict[str, float] = {
    "info": 0.0,
    "warn": 0.5,
    "block": 1.0,
}

_WORD_RE = re.compile(r"[a-z0-9_./:-]+")


@dataclass(frozen=True)
class RankedPak:
    """One ranked Pak row plus transparent score evidence."""

    row: dict[str, Any]
    rank: int
    score: float
    score_reasons: tuple[str, ...]
    score_risks: tuple[str, ...]


def rank_paks(
    rows: Iterable[Mapping[str, Any]],
    *,
    query: str = "",
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Return Pak rows ordered by deterministic OSS relevance score.

    The output preserves each input row's fields and adds:
    ``_oss_rank``, ``_oss_score``, ``_oss_score_reasons``, and
    ``_oss_score_risks``. Ties keep arrival order.
    """

    ranked: list[RankedPak] = []
    q_terms = _terms(query)
    for idx, row_in in enumerate(rows):
        row = dict(row_in)
        score, reasons, risks = _score_row(row, q_terms)
        ranked.append(
            RankedPak(
                row=row,
                rank=idx,
                score=score,
                score_reasons=tuple(reasons),
                score_risks=tuple(risks),
            )
        )

    ordered = sorted(ranked, key=lambda item: (-item.score, item.rank))
    if limit is not None:
        ordered = ordered[: max(0, int(limit))]

    out: list[dict[str, Any]] = []
    for rank, item in enumerate(ordered):
        row = dict(item.row)
        row["_oss_rank"] = rank
        row["_oss_score"] = round(item.score, 6)
        row["_oss_score_reasons"] = list(item.score_reasons)
        row["_oss_score_risks"] = list(item.score_risks)
        out.append(row)
    return out


def _score_row(row: Mapping[str, Any], q_terms: set[str]) -> tuple[float, list[str], list[str]]:
    score = 0.0
    reasons: list[str] = []
    risks: list[str] = []

    pak_type = _norm(row.get("pak_type"))
    type_weight = PAK_TYPE_WEIGHTS.get(pak_type, 0.35 if pak_type else 0.0)
    if type_weight:
        score += type_weight
        reasons.append(f"pak_type:{pak_type}+{type_weight:g}")

    authority = _norm(row.get("authority"))
    authority_weight = AUTHORITY_WEIGHTS.get(authority, 0.25 if authority else 0.0)
    if authority_weight:
        score += authority_weight
        reasons.append(f"authority:{authority}+{authority_weight:g}")

    text_weight = _text_match_weight(row, q_terms)
    if text_weight:
        score += text_weight
        reasons.append(f"text_match+{text_weight:g}")

    for entry in _reason_entries(row):
        code = _norm(entry.get("reason_code") or entry.get("code"))
        base_weight = _float(entry.get("weight"), default=1.0)
        base_weight = max(0.0, min(base_weight, 1.0))
        mult = REASON_CODE_MULTIPLIERS.get(code, 0.5 if code else 0.0)
        contribution = base_weight * mult
        if contribution:
            score += contribution
            reasons.append(f"reason:{code}+{contribution:g}")

    for entry in _risk_entries(row):
        flag = _norm(entry.get("risk_flag") or entry.get("flag"))
        severity = _norm(entry.get("severity")) or "warn"
        penalty = RISK_SEVERITY_PENALTIES.get(severity, 0.0)
        if penalty:
            score -= penalty
            risks.append(f"risk:{flag or 'unknown'}:{severity}-{penalty:g}")

    return score, reasons, risks


def _text_match_weight(row: Mapping[str, Any], q_terms: set[str]) -> float:
    if not q_terms:
        return 0.0
    title_terms = _terms(str(row.get("title") or row.get("name") or ""))
    summary_terms = _terms(str(row.get("summary") or ""))
    id_terms = _terms(str(row.get("pak_id") or row.get("id") or ""))
    title_hits = len(q_terms & title_terms)
    summary_hits = len(q_terms & summary_terms)
    id_hits = len(q_terms & id_terms)
    return min(2.0, title_hits * 0.45 + summary_hits * 0.25 + id_hits * 0.1)


def _reason_entries(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    entries = row.get("_reason_code_entries")
    if isinstance(entries, Sequence) and not isinstance(entries, (str, bytes)):
        return [dict(e) for e in entries if isinstance(e, Mapping)]
    codes = row.get("_reason_codes") or row.get("reason_codes") or []
    if not isinstance(codes, Sequence) or isinstance(codes, (str, bytes)):
        return []
    return [{"reason_code": str(code), "weight": 1.0} for code in codes]


def _risk_entries(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    entries = row.get("_risk_flag_entries")
    if isinstance(entries, Sequence) and not isinstance(entries, (str, bytes)):
        return [dict(e) for e in entries if isinstance(e, Mapping)]
    flags = row.get("_risk_flags") or row.get("risk_flags") or []
    if not isinstance(flags, Sequence) or isinstance(flags, (str, bytes)):
        return []
    return [{"risk_flag": str(flag), "severity": "warn"} for flag in flags]


def _terms(text: str) -> set[str]:
    return {m.group(0).lower() for m in _WORD_RE.finditer(text.lower()) if len(m.group(0)) > 2}


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def _float(value: Any, *, default: float) -> float:
    try:
        if isinstance(value, bool):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


__all__ = [
    "AUTHORITY_WEIGHTS",
    "PAK_TYPE_WEIGHTS",
    "REASON_CODE_MULTIPLIERS",
    "RISK_SEVERITY_PENALTIES",
    "RankedPak",
    "rank_paks",
]
