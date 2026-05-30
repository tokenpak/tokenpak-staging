# SPDX-License-Identifier: Apache-2.0
"""Additive native-status-module config for ``tokenpak codex``.

Configures Codex's *own* built-in status surfaces — ``[tui].status_line``,
``[tui].status_line_use_colors`` and ``[tui].terminal_title`` — which are
**item-based** in Codex 0.134 (a fixed menu of built-in item IDs).  Codex
exposes no freeform/custom statusline text, no command-backed renderer, no
template title, and no hook-set session title, so this module does NOT and
cannot reproduce a Claude-style custom telemetry line.  It only switches on
Codex's native modules — "TokenPak-enhanced Codex native status modules",
not "PakLine parity".

Design rules (intentionally boring):
- **Additive only.**  A key is written *only if absent*.  An existing
  user-set ``status_line`` / ``status_line_use_colors`` / ``terminal_title``
  is never overwritten (unless ``force=True``).
- **Non-destructive write.**  We never re-serialize the whole TOML (which
  would drop the user's comments / formatting).  Missing keys are inserted as
  text — appended as a fresh ``[tui]`` block when no ``[tui]`` exists, or
  inserted right after the existing ``[tui]`` header line otherwise.  All
  other content is preserved byte-for-byte.
- **Backup first.**  A ``<config>.bak`` copy is written before any edit.
- **Stdlib only.**  Read via ``tomllib`` (3.11+) / ``tomli`` (3.10) for
  detection; no TOML *writer* dependency.

EXPERIMENTAL ITEM IDS — NOT MERGE-READY.  The defaults below are the most
defensible tokens evidenced WITHOUT a live confirmation:
- ``model`` / ``git-branch`` appear in Codex's own ``terminal_title`` item list
  and are backtick-quoted in the binary.
- ``codex doctor --strict-config`` does NOT validate ``[tui]`` item values (it
  accepts a deliberately-bogus item), so it cannot confirm the enum.
- The authoritative ``status_line`` item enum is built by serde at runtime (not
  a static string) and is only surfaced by the interactive ``/status`` setup —
  which was blocked during drafting (live session held the state lock; the TUI
  stalls under a synthetic PTY).
Earlier snake_case guesses (``model_name``, ``token_count``, ``rate_limit`` …)
are now *disconfirmed* — the real tokens are kebab/short forms.  Candidate
superset to confirm against a live ``/status`` setup before merge:
``model, status, cwd, directory, usage, git-branch, task-progress, project,
thread, app-name, spinner``.  Correct the two constants below once confirmed.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

try:  # 3.11+
    import tomllib as _toml
except ModuleNotFoundError:  # 3.10
    try:
        import tomli as _toml  # type: ignore
    except ModuleNotFoundError:  # pragma: no cover - detection degrades gracefully
        _toml = None  # type: ignore

# --- Native item sets (EXPERIMENTAL — confirm against live `/status`; see docstring) ---
# Built-in Codex status items only.  No freeform text, no 📦, no semantic task
# text — those are not supported natively and are explicitly out of scope.
# Conservatively limited to the two highest-confidence tokens until the enum is
# confirmed; widen to the documented candidate superset post-confirmation.
DEFAULT_STATUS_ITEMS: list[str] = [
    "model",
    "git-branch",
]
DEFAULT_TITLE_ITEMS: list[str] = [
    "model",
    "git-branch",
]

_MANAGED_KEYS = ("status_line", "status_line_use_colors", "terminal_title")
_TUI_HEADER_RE = re.compile(r"^\s*\[tui\]\s*$")


def default_config_path() -> Path:
    """Resolve Codex's config path, honoring ``CODEX_HOME``."""
    home = os.environ.get("CODEX_HOME")
    base = Path(home) if home else Path.home() / ".codex"
    return base / "config.toml"


def _toml_array(items: list[str]) -> str:
    return "[" + ", ".join(f'"{i}"' for i in items) + "]"


def _existing_tui_keys(text: str) -> set[str]:
    """Return which managed keys already exist under ``tui`` (any TOML form)."""
    if _toml is None:
        # No parser: fall back to a conservative regex scan so we never clobber.
        found: set[str] = set()
        for key in _MANAGED_KEYS:
            if re.search(rf"(^|\n)\s*(tui\.)?{re.escape(key)}\s*=", text):
                found.add(key)
        return found
    try:
        data = _toml.loads(text)
    except Exception:
        # Unparseable config — treat every managed key as "present" so we
        # refuse to touch a file we don't understand.
        return set(_MANAGED_KEYS)
    tui = data.get("tui")
    if not isinstance(tui, dict):
        return set()
    return {k for k in _MANAGED_KEYS if k in tui}


def _has_tui_header(text: str) -> bool:
    return any(_TUI_HEADER_RE.match(line) for line in text.splitlines())


def _tui_exists(text: str) -> bool:
    """True if a ``tui`` table exists in any form (header or dotted keys)."""
    if _toml is None:
        return bool(re.search(r"(^|\n)\s*(\[tui\]|tui\.[a-z])", text))
    try:
        return isinstance(_toml.loads(text).get("tui"), dict)
    except Exception:
        return True  # unknown shape → assume present, stay non-destructive


def _render_key(key: str, status_items: list[str], title_items: list[str], use_colors: bool) -> str:
    if key == "status_line":
        return f"status_line = {_toml_array(status_items)}"
    if key == "terminal_title":
        return f"terminal_title = {_toml_array(title_items)}"
    if key == "status_line_use_colors":
        return f"status_line_use_colors = {'true' if use_colors else 'false'}"
    raise ValueError(key)  # pragma: no cover


def _insert_after_tui_header(text: str, lines: list[str]) -> str:
    out: list[str] = []
    inserted = False
    for line in text.splitlines():
        out.append(line)
        if not inserted and _TUI_HEADER_RE.match(line):
            out.extend(lines)
            inserted = True
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def _append_tui_block(text: str, lines: list[str]) -> str:
    block = "\n[tui]\n" + "\n".join(lines) + "\n"
    if text and not text.endswith("\n"):
        text += "\n"
    return text + block


def _append_dotted(text: str, lines: list[str]) -> str:
    # tui exists in dotted form with no [tui] header — appending a [tui] block
    # would redefine the namespace (TOML error), so append dotted keys instead.
    dotted = "\n".join(f"tui.{ln}" for ln in lines) + "\n"
    if text and not text.endswith("\n"):
        text += "\n"
    return text + dotted


def install_status_line(
    config_path: str | os.PathLike[str] | None = None,
    *,
    status_items: list[str] | None = None,
    title_items: list[str] | None = None,
    use_colors: bool = True,
) -> dict:
    """Additively enable Codex native status modules.

    Strictly additive: a managed key is written only if absent.  An existing
    user-set ``status_line`` / ``status_line_use_colors`` / ``terminal_title``
    is never overwritten.  Returns a result dict
    ``{"path", "added", "skipped", "changed", "backup"}``.
    """
    path = Path(config_path) if config_path is not None else default_config_path()
    status_items = list(status_items or DEFAULT_STATUS_ITEMS)
    title_items = list(title_items or DEFAULT_TITLE_ITEMS)

    text = path.read_text(encoding="utf-8") if path.exists() else ""
    existing = _existing_tui_keys(text)

    to_add = [k for k in _MANAGED_KEYS if k not in existing]
    skipped = [k for k in _MANAGED_KEYS if k in existing]
    result = {
        "path": str(path),
        "added": to_add,
        "skipped": skipped,
        "changed": False,
        "backup": None,
    }
    if not to_add:
        return result  # idempotent / fully user-owned → no-op

    new_lines = [_render_key(k, status_items, title_items, use_colors) for k in to_add]
    if _has_tui_header(text):
        new_text = _insert_after_tui_header(text, new_lines)
    elif _tui_exists(text):
        new_text = _append_dotted(text, new_lines)  # dotted form already in use
    else:
        new_text = _append_tui_block(text, new_lines)

    # Backup before any edit (only when an original file exists).
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = path.with_suffix(path.suffix + ".bak")
        backup.write_text(text, encoding="utf-8")
        result["backup"] = str(backup)

    # Atomic write.
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(new_text, encoding="utf-8")
    os.replace(tmp, path)
    result["changed"] = True
    return result


def main(argv: list[str] | None = None) -> int:
    """CLI entry: ``tokenpak codex statusline [install|show] [--no-colors] [--config PATH]``."""
    argv = list(argv if argv is not None else sys.argv[1:])
    use_colors = "--no-colors" not in argv
    cfg = None
    if "--config" in argv:
        i = argv.index("--config")
        cfg = argv[i + 1] if i + 1 < len(argv) else None
    verb = next((a for a in argv if not a.startswith("-") and a != cfg), "install")

    if verb == "show":
        path = Path(cfg) if cfg else default_config_path()
        print(f"config: {path}")
        print(f"status items (default): {DEFAULT_STATUS_ITEMS}")
        print(f"title items (default):  {DEFAULT_TITLE_ITEMS}")
        if _toml is not None and path.exists():
            print(f"existing tui keys: {sorted(_existing_tui_keys(path.read_text()))}")
        return 0

    if verb != "install":
        print("usage: tokenpak codex statusline [install|show] [--no-colors] [--config PATH]")
        return 2

    res = install_status_line(cfg, use_colors=use_colors)
    if res["changed"]:
        print(f"tokenpak: enabled native status modules — added {res['added']} ({res['path']})", file=sys.stderr)
        if res["backup"]:
            print(f"tokenpak: backup written ({res['backup']})", file=sys.stderr)
    else:
        msg = "already configured" if res["skipped"] else "no changes"
        print(f"tokenpak: native status modules {msg} ({res['path']}); skipped {res['skipped']}", file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
