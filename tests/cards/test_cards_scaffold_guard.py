# SPDX-License-Identifier: Apache-2.0
"""Scaffold layout + package-tree guard tests (Std 54 §J).

The non-negotiable invariant: ``tokenpak cards scaffold`` writes into
the PROJECT tree only — never inside the installed ``tokenpak`` package
(pip users do not own that tree).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tokenpak.cards.model import CardError
from tokenpak.cards.parser import parse_card_file
from tokenpak.cards.scaffold import (
    assert_outside_package_tree,
    package_root,
    scaffold_card,
)
from tokenpak.cards.validate import validate_card

# ---------------------------------------------------------------------------
# §J layout
# ---------------------------------------------------------------------------


def test_tip_scaffold_creates_spec_layout(tmp_path):
    created = scaffold_card(card_type="tip", name="acme", project_root=tmp_path)
    rels = {str(p.relative_to(tmp_path)) for p in created}
    assert rels == {
        "integrations/acme/acme.tip.md",
        "integrations/acme/adapter.py",
        "integrations/acme/fixtures",
    }
    assert (tmp_path / "integrations/acme/fixtures").is_dir()


def test_pak_scaffold_creates_paks_layout(tmp_path):
    created = scaffold_card(card_type="pak", name="notes", project_root=tmp_path)
    assert [str(p.relative_to(tmp_path)) for p in created] == ["paks/notes.pak.md"]


def test_scaffolded_cards_validate_cleanly(tmp_path):
    scaffold_card(card_type="tip", name="acme", project_root=tmp_path)
    scaffold_card(card_type="pak", name="notes", project_root=tmp_path)
    tip_report = validate_card(
        parse_card_file(tmp_path / "integrations/acme/acme.tip.md")
    )
    pak_report = validate_card(parse_card_file(tmp_path / "paks/notes.pak.md"))
    assert tip_report.ok, [i.message for i in tip_report.issues]
    assert pak_report.ok, [i.message for i in pak_report.issues]


def test_scaffold_refuses_overwrite(tmp_path):
    scaffold_card(card_type="pak", name="notes", project_root=tmp_path)
    with pytest.raises(CardError, match="refusing to overwrite"):
        scaffold_card(card_type="pak", name="notes", project_root=tmp_path)


def test_scaffold_rejects_worker_type(tmp_path):
    with pytest.raises(CardError, match="Phase 2"):
        scaffold_card(card_type="worker", name="helper", project_root=tmp_path)
    assert not list(tmp_path.iterdir())


def test_scaffold_rejects_phase2_tip_kind(tmp_path):
    with pytest.raises(CardError, match="not scaffoldable in Phase 1"):
        scaffold_card(
            card_type="tip", name="acme", kind="context_connector",
            project_root=tmp_path,
        )
    assert not list(tmp_path.iterdir())


def test_scaffold_rejects_bad_name(tmp_path):
    with pytest.raises(CardError, match="invalid card name"):
        scaffold_card(card_type="pak", name="../escape", project_root=tmp_path)
    assert not list(tmp_path.iterdir())


# ---------------------------------------------------------------------------
# §J guard — MUST NOT write inside the installed package tree
# ---------------------------------------------------------------------------


def test_guard_rejects_paths_inside_package_tree():
    pkg = package_root()
    with pytest.raises(CardError, match="installed package tree"):
        assert_outside_package_tree(pkg / "services" / "integrations")
    with pytest.raises(CardError, match="installed package tree"):
        assert_outside_package_tree(pkg)


def test_guard_rejects_site_packages_paths(tmp_path):
    fake = tmp_path / "venv" / "lib" / "site-packages" / "someproject"
    with pytest.raises(CardError, match="installed package location"):
        assert_outside_package_tree(fake)


def test_guard_allows_project_paths(tmp_path):
    assert_outside_package_tree(tmp_path / "integrations" / "acme")  # no raise


def test_scaffold_never_writes_inside_package_tree():
    """Scaffolding with the package itself as project root must refuse
    before any filesystem write (Std 54 §J guard test)."""
    pkg = package_root()
    before = set(pkg.rglob("*.tip.md")) | set(pkg.rglob("*.pak.md"))
    with pytest.raises(CardError, match="Std 54 §J"):
        scaffold_card(card_type="tip", name="sneaky", project_root=pkg)
    with pytest.raises(CardError, match="Std 54 §J"):
        scaffold_card(card_type="pak", name="sneaky", project_root=pkg / "services")
    after = set(pkg.rglob("*.tip.md")) | set(pkg.rglob("*.pak.md"))
    assert before == after
    # No scaffold output landed anywhere in the package tree. (Note:
    # tokenpak/integrations/ pre-exists as a source package — assert on
    # the scaffold name, not the directory.)
    assert not list(pkg.rglob("*sneaky*"))
    assert not (pkg / "services" / "paks").exists()


def test_package_root_is_the_tokenpak_package():
    assert package_root().name == "tokenpak"
    assert (package_root() / "__init__.py").is_file()
