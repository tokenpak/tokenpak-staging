# SPDX-License-Identifier: Apache-2.0
"""Deterministic local Pak ranking for the OSS recall store.

This is PakRank Lite: transparent scoring over rows the user already controls
in the local recall/Pak store. It does not capture sessions, hydrate anchors,
assemble Context Packages, enforce policy refusals, or call a Pro daemon.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Optional

_TOKEN_RE = re.compile(r"[a-z0-9_./:-]+")

_TYPE_WEIGHTS = {
    "decision": 1.0,
    "session": 0.95,
    "interaction": 0.9,
    "vault": 0.85,
    "imported": 0.8,
    "recall": 0.75,
}

_AUTHORITY_WEIGHTS = {
    "user_approved": 1.0,
    "file_source": 0.9,
    "tool_result": 0.85,
    "llm_generated": 0.75,
    "derived": 0.7,
}

_RISK_SEVERITY_WEIGHTS = {
    "info": 0.95,
    "warn": 0.7,
    "block": 0.25,
}


@dataclass(frozen=True)
class RankedPak:
    """One ranked Pak row with an explainable score."""

    rank: int
    pak: dict[str, Any]
    score: float
    score_reasons: dict[str, float]


def rank_paks(
    rows: list[Mapping[str, Any]],
    *,
    query: Optional[str] = None,
    limit: Optional[int] = None,
) -> list[RankedPak]:
    """Rank local recall/Pak rows deterministically.

    The formula intentionally uses only OSS-visible metadata: Pak type,
    authority, title/summary text, reason-code weights, risk-flag severity,
    recency, and stable tie order. Scores are comparable within one result
    set; they are not a public quality or savings claim.
    """
    copied = [dict(row) for row in rows]
    recency = _recency_scores(copied)
    query_tokens = _tokenize(query or "")

    scored: list[tuple[float, str, str, dict[str, Any], dict[str, float]]] = []
    for row in copied:
        pak_id = str(row.get("pak_id") or row.get("id") or "")
        type_score = _weight(_TYPE_WEIGHTS, row.get("pak_type"), default=0.6)
        authority_score = _weight(_AUTHORITY_WEIGHTS, row.get("authority"), default=0.6)
        reason_score = _reason_score(row)
        risk_score = _risk_score(row)
        text_score = _text_score(row, query_tokens)
        recency_score = recency.get(pak_id, 0.0)

        reasons = {
            "type": type_score,
            "authority": authority_score,
            "reason_codes": reason_score,
            "risk_flags": risk_score,
            "text": text_score,
            "recency": recency_score,
        }
        score = (
            0.20 * type_score
            + 0.15 * authority_score
            + 0.25 * reason_score
            + 0.20 * text_score
            + 0.10 * risk_score
            + 0.10 * recency_score
        )
        scored.append(
            (
                round(score, 6),
                _row_timestamp(row),
                pak_id,
                row,
                {k: round(v, 6) for k, v in reasons.items()},
            )
        )

    scored.sort(key=lambda item: (-item[0], item[1], item[2]))
    if limit is not None:
        scored = scored[: max(0, int(limit))]

    return [
        RankedPak(
            rank=i + 1,
            pak=row,
            score=score,
            score_reasons=reasons,
        )
        for i, (score, _ts, _pak_id, row, reasons) in enumerate(scored)
    ]


def _weight(table: dict[str, float], value: Any, *, default: float) -> float:
    if value is None:
        return default
    return table.get(str(value).strip().lower(), default)


def _reason_score(row: Mapping[str, Any]) -> float:
    weights = row.get("_reason_weights") or {}
    if isinstance(weights, Mapping) and weights:
        return max(_clamp01(float(v)) for v in weights.values())
    codes = row.get("_reason_codes") or []
    if codes:
        return 0.75
    return 0.5


def _risk_score(row: Mapping[str, Any]) -> float:
    severities = row.get("_risk_severities") or {}
    if isinstance(severities, Mapping) and severities:
        return min(
            _RISK_SEVERITY_WEIGHTS.get(str(v).strip().lower(), 0.7)
            for v in severities.values()
        )
    flags = row.get("_risk_flags") or []
    if flags:
        return 0.7
    return 1.0


def _text_score(row: Mapping[str, Any], query_tokens: set[str]) -> float:
    text = " ".join(
        str(row.get(k) or "")
        for k in (
            "title",
            "summary",
            "pak_type",
            "source_type",
            "authority",
            "project",
            "topic",
        )
    )
    row_tokens = _tokenize(text)
    if not query_tokens:
        return 0.5 if row_tokens else 0.0
    if not row_tokens:
        return 0.0
    return len(query_tokens & row_tokens) / len(query_tokens)


def _recency_scores(rows: list[Mapping[str, Any]]) -> dict[str, float]:
    ordered = sorted(
        rows,
        key=lambda row: (_row_timestamp(row), str(row.get("pak_id") or row.get("id") or "")),
        reverse=True,
    )
    if not ordered:
        return {}
    denom = max(len(ordered) - 1, 1)
    scores: dict[str, float] = {}
    for idx, row in enumerate(ordered):
        pak_id = str(row.get("pak_id") or row.get("id") or "")
        scores[pak_id] = 1.0 - (idx / denom)
    return scores


def _row_timestamp(row: Mapping[str, Any]) -> str:
    raw = str(row.get("updated_at") or row.get("created_at") or row.get("ts") or "")
    if not raw:
        return ""
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return parsed.isoformat()
    except ValueError:
        return raw


def _tokenize(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(text.lower()))


def _clamp01(value: float) -> float:
    return min(1.0, max(0.0, value))


__all__ = ["RankedPak", "rank_paks"]
