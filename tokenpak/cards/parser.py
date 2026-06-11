# SPDX-License-Identifier: Apache-2.0
"""Card source parser — frontmatter + opaque body (Std 54 §A/§C).

Security contract (invariant 2: *raw Markdown is never executed*):

* Frontmatter is parsed with ``yaml.safe_load`` only. Python-object YAML
  tags (``!!python/...``) raise a parse error — they never construct
  objects.
* The Markdown body is kept as opaque text. It is never executed,
  imported, eval'd, or rendered through a templating engine.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Optional

from tokenpak.cards.model import CardError, ParsedCard

_FM_DELIM = "---"

# Static env-var reference scan for `cards inspect` (Std 54 §I — env-var
# references are part of the static declarations surface). Matches
# ``${NAME}`` and bare ``$NAME`` (uppercase, underscore) tokens.
_ENV_REF_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Z_][A-Z0-9_]*)\b")


def split_frontmatter(text: str) -> tuple[str, str]:
    """Split card text into (frontmatter_yaml, body).

    The card must open with a ``---`` line and contain a closing ``---``
    line. Raises :class:`CardError` when the frontmatter block is absent
    or unterminated.
    """
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != _FM_DELIM:
        raise CardError(
            "card has no YAML frontmatter block (must start with '---')"
        )
    for i in range(1, len(lines)):
        if lines[i].strip() == _FM_DELIM:
            fm = "".join(lines[1:i])
            body = "".join(lines[i + 1:])
            return fm, body
    raise CardError("card frontmatter block is unterminated (missing closing '---')")


def parse_card_text(text: str, *, path: Optional[str] = None) -> ParsedCard:
    """Parse card source text into a :class:`ParsedCard`.

    Parsing is structural only — no semantic validation happens here
    (see :func:`tokenpak.cards.validate.validate_card`).
    """
    import yaml

    fm_text, body = split_frontmatter(text)
    try:
        data = yaml.safe_load(fm_text)
    except yaml.YAMLError as exc:
        raise CardError(f"card frontmatter is not valid YAML: {exc}") from exc
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise CardError(
            f"card frontmatter must be a YAML mapping, got {type(data).__name__}"
        )
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return ParsedCard(
        path=path,
        source_text=text,
        source_sha256=sha,
        frontmatter=data,
        body=body,
    )


def parse_card_file(path: str | Path) -> ParsedCard:
    """Read and parse a card file from disk."""
    p = Path(path)
    if not p.is_file():
        raise CardError(f"card file not found: {p}")
    try:
        text = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise CardError(f"cannot read card file {p}: {exc}") from exc
    return parse_card_text(text, path=str(p))


def scan_env_references(card: ParsedCard) -> list[str]:
    """Static scan of the raw card source for env-var references.

    Purely textual — supports the ``cards inspect`` static-declarations
    view (Std 54 §I). No environment access happens here.
    """
    refs: set[str] = set()
    for m in _ENV_REF_RE.finditer(card.source_text):
        refs.add(m.group(1) or m.group(2))
    return sorted(refs)


__all__ = [
    "parse_card_file",
    "parse_card_text",
    "scan_env_references",
    "split_frontmatter",
]
