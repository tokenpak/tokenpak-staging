# SPDX-License-Identifier: Apache-2.0
"""Card scaffolding (Std 54 §J).

Scaffolds into the **project**, never into the installed package tree:

    tokenpak cards scaffold --type tip --kind provider_adapter --name acme

    creates:
        integrations/acme/acme.tip.md       # metadata card
        integrations/acme/adapter.py        # credential-provider stub
        integrations/acme/fixtures/         # test fixtures

The non-negotiable §J invariant: ``tokenpak cards scaffold`` MUST NOT
write inside ``tokenpak/...`` or any path within the installed package —
pip users do not own that tree. :func:`assert_outside_package_tree`
enforces this before any write and is covered by a guard test.
"""

from __future__ import annotations

import re
from pathlib import Path

from tokenpak.cards.model import (
    PROJECT_PAK_DIR,
    PROJECT_TIP_DIR,
    SOURCE_FORMAT,
    TARGET_CONTRACT_PAK,
    TARGET_CONTRACT_PROVIDER_ADAPTER,
    CardError,
)

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def package_root() -> Path:
    """Filesystem root of the installed ``tokenpak`` package."""
    import tokenpak

    return Path(tokenpak.__file__).resolve().parent


def assert_outside_package_tree(target: Path) -> None:
    """Refuse any scaffold target inside the installed package tree (§J).

    Blocks both the resolved ``tokenpak`` package directory and any
    ``site-packages`` / ``dist-packages`` path.
    """
    resolved = target.resolve()
    pkg = package_root()
    if resolved == pkg or pkg in resolved.parents:
        raise CardError(
            f"refusing to scaffold inside the installed package tree: {resolved} "
            "(Std 54 §J — scaffold writes into the project only)"
        )
    if any(part in ("site-packages", "dist-packages") for part in resolved.parts):
        raise CardError(
            f"refusing to scaffold inside an installed package location: {resolved} "
            "(Std 54 §J — scaffold writes into the project only)"
        )


def scaffold_card(
    *,
    card_type: str,
    name: str,
    kind: str = "provider_adapter",
    project_root: Path,
) -> list[Path]:
    """Scaffold a new card into ``project_root``. Returns created paths.

    Refuses to overwrite existing files and refuses any target inside
    the installed package tree (§J).
    """
    if not _NAME_RE.match(name or ""):
        raise CardError(
            f"invalid card name {name!r} — use a lowercase slug "
            "(letters/digits/dot/dash/underscore)"
        )
    if card_type == "tip":
        if kind != "provider_adapter":
            raise CardError(
                f"tip_kind {kind!r} is not scaffoldable in Phase 1 — only "
                "provider_adapter has a canonical contract (Std 54 §B)"
            )
        return _scaffold_tip(name, project_root)
    if card_type == "pak":
        return _scaffold_pak(name, project_root)
    if card_type == "worker":
        raise CardError(
            "--type worker is Phase 2 — WorkerProfile has no canonical contract yet (Std 54 §B)"
        )
    raise CardError(f"unknown --type {card_type!r} (expected tip|pak)")


def _scaffold_tip(name: str, project_root: Path) -> list[Path]:
    base = project_root / PROJECT_TIP_DIR / name
    assert_outside_package_tree(base)
    card_path = base / f"{name}.tip.md"
    adapter_path = base / "adapter.py"
    fixtures_dir = base / "fixtures"
    for p in (card_path, adapter_path):
        if p.exists():
            raise CardError(
                f"refusing to overwrite existing file: {p}. "
                "Choose a different --name, or remove the file first."
            )

    base.mkdir(parents=True, exist_ok=True)
    fixtures_dir.mkdir(parents=True, exist_ok=True)
    (fixtures_dir / ".gitkeep").write_text("", encoding="utf-8")
    card_path.write_text(_tip_card_template(name), encoding="utf-8")
    adapter_path.write_text(_adapter_stub_template(name), encoding="utf-8")
    return [card_path, adapter_path, fixtures_dir]


def _scaffold_pak(name: str, project_root: Path) -> list[Path]:
    base = project_root / PROJECT_PAK_DIR
    assert_outside_package_tree(base)
    card_path = base / f"{name}.pak.md"
    if card_path.exists():
        raise CardError(
            f"refusing to overwrite existing file: {card_path}. "
            "Choose a different --name, or remove the file first."
        )
    base.mkdir(parents=True, exist_ok=True)
    card_path.write_text(_pak_card_template(name), encoding="utf-8")
    return [card_path]


# ---------------------------------------------------------------------------
# Templates — public-safe placeholder text only
# ---------------------------------------------------------------------------


def _tip_card_template(name: str) -> str:
    return f"""---
card_kind: tip
tip_kind: provider_adapter
name: {name}
target_contract: {TARGET_CONTRACT_PROVIDER_ADAPTER}
source_format: {SOURCE_FORMAT}
card_status: draft
tokenpak_min_version: "1.5.0"

# Capability strings come from tokenpak/tip/capabilities.py only.
# Do not invent capability strings inline (Std 54 §E).
capabilities: []
---

# {name} provider adapter

Describe what this provider adapter integrates with and how its
credentials are sourced. This Markdown body is authoring documentation —
it is never executed; only the validated compiled manifest is a runtime
input.

Validate and compile with:

    tokenpak cards validate {PROJECT_TIP_DIR}/{name}/{name}.tip.md
    tokenpak cards compile {PROJECT_TIP_DIR}/{name}/{name}.tip.md
"""


def _adapter_stub_template(name: str) -> str:
    from tokenpak.cards.compile import derive_provider_class_name

    class_name = derive_provider_class_name(name)
    return f'''# SPDX-License-Identifier: Apache-2.0
"""Credential-provider stub for ``{name}``.

Generated by ``tokenpak cards scaffold``. Fill in the credential read +
injection-plan logic, then register the adapter through your project's
adapter-registration entrypoint (see the provider adapter standard,
the spec, for the naming and registration contract).

The ``capabilities`` frozenset below is read statically by
``tokenpak cards validate`` for the card-to-adapter consistency check —
keep it in sync with the capabilities declared in ``{name}.tip.md``.
"""

from __future__ import annotations


class {class_name}:
    """Provider adapter for ``{name}`` (fill in the credential logic)."""

    name = "{name}"

    # Declare only labels from tokenpak.tip.capabilities (or
    # ext.<vendor>.* for vendor extension hints).
    capabilities: frozenset[str] = frozenset()

    def adapter_info(self) -> dict:
        return {{
            "name": self.name,
            "capabilities": sorted(self.capabilities),
        }}
'''


def _pak_card_template(name: str) -> str:
    return f"""---
card_kind: pak
name: {name}
target_contract: {TARGET_CONTRACT_PAK}
source_format: {SOURCE_FORMAT}
card_status: draft
tokenpak_min_version: "1.5.0"

# Pak-specific canonical fields (compile directly to PakXxx types)
pak_subtype: recall
pak_status: proposed
authority: user_approved
confidence: medium
retention: session
privacy: local_only

# Card-only authoring metadata (no runtime effect today)
category_tags: []
advisory_risk_flags: []
---

# {name}

Write the knowledge this Pak should carry here. The first H1 becomes
the Pak title and the body text becomes the Pak summary at compile
time. This Markdown is never executed.

Validate and compile with:

    tokenpak cards validate {PROJECT_PAK_DIR}/{name}.pak.md
    tokenpak cards compile {PROJECT_PAK_DIR}/{name}.pak.md
"""


__all__ = [
    "assert_outside_package_tree",
    "package_root",
    "scaffold_card",
]
