# SPDX-License-Identifier: Apache-2.0
"""Card discovery + installed-manifest store (Std 54 §G/§K).

Project layout (Std 54 §K):

* committed sources: ``integrations/**/*.tip.md``, ``paks/**/*.pak.md``,
  ``.tokenpak.md`` (project index — NOT a card)
* generated state (gitignored): ``.tokenpak/cache/cards/compiled/`` and
  ``.tokenpak/cache/cards/installed.json``

User-global state lives under ``~/.tpk/cards/`` per Std 54 §K
and resolves through :func:`tokenpak._paths.under`. Phase 1 writes only
the project-local store; the user-global tree is read for listing and
diagnostics.

Trust levels (Std 54 §G): ``dev`` discovery scans the project tree
(warnings allowed; the ``.tokenpak.md`` index gates runtime loading);
``locked`` mode loads installed cards only — no project-tree discovery.
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Any, Optional

from tokenpak.cards.model import (
    CARD_SUFFIXES,
    PROJECT_COMPILED_SUBPATH,
    PROJECT_INDEX_FILENAME,
    PROJECT_INSTALLED_SUBPATH,
    PROJECT_PAK_DIR,
    PROJECT_STATE_DIR,
    PROJECT_TIP_DIR,
    CardError,
)

# ---------------------------------------------------------------------------
# Project paths
# ---------------------------------------------------------------------------


def project_index_path(root: Path) -> Path:
    return root / PROJECT_INDEX_FILENAME


def has_project_index(root: Path) -> bool:
    return project_index_path(root).is_file()


def compiled_dir(root: Path) -> Path:
    return root.joinpath(PROJECT_STATE_DIR, *PROJECT_COMPILED_SUBPATH)


def installed_manifest_path(root: Path) -> Path:
    return root.joinpath(PROJECT_STATE_DIR, *PROJECT_INSTALLED_SUBPATH)


def user_global_cards_dir() -> Optional[Path]:
    """``~/.tpk/cards/`` via the canonical resolver (Std 54 §K).

    Returns None when the resolver cannot produce the path (e.g. the
    cards subdir is not adopted in this build) — callers treat the
    user-global tree as best-effort, read-only state in Phase 1.
    """
    try:
        from tokenpak import _paths

        return _paths.under("cards")
    except (ValueError, ImportError):
        return None


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def card_kind_for_path(path: Path) -> Optional[str]:
    """Map a filename to its card kind by suffix (``.tip.md`` / ``.pak.md``)."""
    name = path.name
    for suffix, kind in CARD_SUFFIXES.items():
        if name.endswith(suffix):
            return kind
    return None


def discover_cards(root: Path, *, card_type: Optional[str] = None) -> list[Path]:
    """Find card source files in a project tree (Std 54 §L ``discover``).

    Scans the §K layout directories (``integrations/`` for ``.tip.md``,
    ``paks/`` for ``.pak.md``) plus the project root one level deep for
    stray cards, skipping dotted directories and the generated state
    tree. Results are sorted for determinism.
    """
    found: set[Path] = set()
    scan_specs = [
        (root / PROJECT_TIP_DIR, "*.tip.md"),
        (root / PROJECT_PAK_DIR, "*.pak.md"),
    ]
    for base, pattern in scan_specs:
        if base.is_dir():
            for p in base.rglob(pattern):
                if _skippable(p, root):
                    continue
                found.add(p)
    # Root-level stray cards (authoring convenience; flagged by doctor).
    for pattern in ("*.tip.md", "*.pak.md"):
        for p in root.glob(pattern):
            if p.name == PROJECT_INDEX_FILENAME:
                continue
            found.add(p)

    results = sorted(found)
    if card_type:
        results = [p for p in results if card_kind_for_path(p) == card_type]
    return results


def _skippable(path: Path, root: Path) -> bool:
    try:
        rel = path.relative_to(root)
    except ValueError:
        return True
    return any(part.startswith(".") for part in rel.parts[:-1])


# ---------------------------------------------------------------------------
# Installed manifest (project-local activation state, Std 54 §K)
# ---------------------------------------------------------------------------


def load_installed(root: Path) -> dict[str, Any]:
    """Read the project installed manifest. Missing file → empty store."""
    path = installed_manifest_path(root)
    if not path.is_file():
        return {"version": 1, "cards": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CardError(f"cannot read installed manifest {path}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("cards"), dict):
        raise CardError(f"installed manifest {path} has an unexpected shape")
    return data


def record_install(
    root: Path,
    *,
    name: str,
    kind: str,
    source_path: Path,
    source_sha256: str,
    compiled_path: Path,
) -> Path:
    """Record a card in the project installed manifest (Std 54 §G/§L)."""
    store = load_installed(root)
    now = (
        datetime.datetime.now(datetime.timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
    store["cards"][name] = {
        "card_kind": kind,
        "source_path": str(source_path),
        "source_sha256": source_sha256,
        "compiled_path": str(compiled_path),
        "installed_at": now,
    }
    path = installed_manifest_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(store, indent=2) + "\n", encoding="utf-8")
    return path


def user_global_installed() -> dict[str, Any]:
    """Read-only view of ``~/.tpk/cards/installed/*.json`` (best-effort)."""
    base = user_global_cards_dir()
    out: dict[str, Any] = {}
    if base is None:
        return out
    installed = base / "installed"
    if not installed.is_dir():
        return out
    for f in sorted(installed.glob("*.json")):
        try:
            out[f.stem] = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            out[f.stem] = {"error": "unreadable"}
    return out


__all__ = [
    "card_kind_for_path",
    "compiled_dir",
    "discover_cards",
    "has_project_index",
    "installed_manifest_path",
    "load_installed",
    "project_index_path",
    "record_install",
    "user_global_cards_dir",
    "user_global_installed",
]
