"""SSRM relevance-drift heuristic.

Phase 1 default: word-set Jaccard. Compares the latest user turn against
a session-anchor (the first user turn observed for this session, or a
short canonical prefix). Returns a similarity score in [0.0, 1.0]; 1.0
means identical, 0.0 means no word overlap.

Embeddings-based drift is deferred to Phase 2+ per parent §13 open-
decision 1; the function signature is stable so Phase 2 can swap the
implementation without changing callers.
"""

from __future__ import annotations

import json
import re

from .fingerprint import canonicalize_user_turn

# Public surface of this module. ``canonicalize_user_turn`` is a re-import from
# ``.fingerprint`` (its owning module) and is internal-but-authored here — it is
# scoped out so the API snapshot records it only under its owner.
__all__ = ["drift_for_body", "drift_score", "jaccard"]

_TOKEN_RE = re.compile(r"[a-z0-9]{2,}")


def _tokens(text: str) -> set[str]:
    return set(_TOKEN_RE.findall((text or "").lower()))


def jaccard(a: str, b: str) -> float:
    """Return |A ∩ B| / |A ∪ B| over word sets. Empty inputs → 1.0 (no drift)."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta and not tb:
        return 1.0
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    union = len(ta | tb)
    return inter / union if union else 1.0


def drift_score(
    current_text: str,
    anchor_text: str,
    *,
    algorithm: str = "jaccard_word_set",
) -> float:
    """Return drift score in [0.0, 1.0]. 1.0 means no drift; 0.0 means total drift.

    `algorithm` is reserved for Phase 2 (e.g. "embedding"). Phase 1 only
    supports "jaccard_word_set" and falls back to it for unknown values.
    """
    if algorithm not in ("jaccard_word_set",):
        algorithm = "jaccard_word_set"
    return jaccard(current_text, anchor_text)


def drift_for_body(
    body: bytes | str | dict,
    anchor_text: str,
    *,
    algorithm: str = "jaccard_word_set",
) -> float:
    """Compute drift between the current request body's last user turn and an anchor."""
    if isinstance(body, bytes):
        try:
            body_obj = json.loads(body.decode("utf-8", errors="replace"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return 1.0
    elif isinstance(body, str):
        try:
            body_obj = json.loads(body)
        except json.JSONDecodeError:
            return 1.0
    elif isinstance(body, dict):
        body_obj = body
    else:
        return 1.0
    current = canonicalize_user_turn(body_obj)
    return drift_score(current, anchor_text, algorithm=algorithm)
