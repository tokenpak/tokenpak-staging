# SPDX-License-Identifier: Apache-2.0
"""Std 36 public-safe sanitization for Dispatch delivery/receipt surfaces.

Standards Delta v0 §10 requires that Dispatch output intended for public
surfaces — Delivery Package summaries and exported Receipts — be sanitized
through the Std 36 public-safe path before display: internal agent names,
internal task-ID prefixes, and machine-local home paths are redacted.

**Provenance note (flagged in the P-CLI-01 report):** at implementation time no
reusable *text* sanitizer for Std 36 public-safe defaults existed in the
codebase. ``tokenpak.security.security.redact_pii`` covers credentials/PII (not
agent names / internal IDs / home paths), and ``sanitize_model_name`` /
``sanitize_cli_arg`` are input validators. The canonical Std 36 enforcement
lives in CI as ``.github/workflows/identity-language-check.yml`` (a checker,
not an importable redactor). This module therefore implements a **minimal,
focused redaction pass** scoped to the Dispatch delivery/receipt surfaces. If a
shared Python sanitizer lands later, these call sites should delegate to it.

The redaction is conservative: it only touches string *values* (keys are left
intact so downstream parsers keep working) and replaces matched tokens with a
neutral ``[redacted]`` marker.
"""

from __future__ import annotations

import re
from typing import Any

# Internal agent names (Std 36 / Standards Delta v0 §10). Word-boundary matched,
# case-insensitive, so "Sue" but not "issue" / "pursue".
_AGENT_NAMES = ("Sue", "Suki", "Trix", "Cali", "Aya", "Dee", "Rei Po", "ReiPo")

# Internal task-ID prefixes (TSR-1234, MTC-09, …). Prefix + dash + alphanumerics.
_TASK_ID_PREFIXES = (
    "TSR", "TPS", "CCI", "MTC", "OAS", "TIP7", "TRIX-MTC", "WS",
)

_REDACTED = "[redacted]"

# Order matters: longer/compound patterns first so "Rei Po" wins over "Po".
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Machine-local home paths → /home/<user>/...  and ~/.claude/projects/...
    (re.compile(r"/home/[^/\s]+/\S*"), _REDACTED),
    (re.compile(r"~/\.claude/projects/\S*"), _REDACTED),
    # Internal task IDs (e.g. TSR-1234, MTC-09).
    (
        re.compile(
            r"\b(?:" + "|".join(re.escape(p) for p in _TASK_ID_PREFIXES) + r")-[A-Za-z0-9]+\b"
        ),
        _REDACTED,
    ),
    # Internal agent names (word-boundary, case-insensitive).
    (
        re.compile(
            r"\b(?:" + "|".join(re.escape(n) for n in _AGENT_NAMES) + r")\b",
            re.IGNORECASE,
        ),
        _REDACTED,
    ),
)


def sanitize_public_text(text: str) -> str:
    """Redact internal agent names, task IDs, and home paths from *text*.

    Idempotent and safe on already-clean text (returns it unchanged). Non-string
    input is returned untouched.
    """
    if not isinstance(text, str) or not text:
        return text
    out = text
    for pattern, replacement in _PATTERNS:
        out = pattern.sub(replacement, out)
    return out


def sanitize_public_obj(obj: Any) -> Any:
    """Recursively sanitize every string *value* in a JSON-like structure.

    Dict keys are left intact (so the shape/contract is preserved); only string
    values, and strings nested in lists/tuples, are passed through
    :func:`sanitize_public_text`. Returns a new structure; the input is not
    mutated.
    """
    if isinstance(obj, str):
        return sanitize_public_text(obj)
    if isinstance(obj, dict):
        return {k: sanitize_public_obj(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [sanitize_public_obj(v) for v in obj]
    return obj


__all__ = ["sanitize_public_text", "sanitize_public_obj"]
