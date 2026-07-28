# SPDX-License-Identifier: Apache-2.0
"""Project scoping for vault retrieval.

The scenario under test is several concurrent projects of the same kind, with
overlapping vocabulary and colliding identifiers (the same PR number in each).
Lexical scoring cannot separate them, so scope has to be resolved before
scoring and enforced as a filter.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tokenpak.vault import config as vault_config
from tokenpak.vault.project_scope import (
    SHARED,
    AmbiguityPolicy,
    ProjectRegistry,
    ScopeConflictError,
)
from tokenpak.vault.sqlite_backend import SQLiteRetrievalBackend

PROJECTS = ("acme-storefront", "bluefin-portal", "corvid-shop", "delta-market", "echo-cart")

# The query that motivated this module: no project named, an identifier that
# collides across every project, and a deictic "the project" that carries no
# disambiguating signal at all.
COLLIDING_QUERY = "audit the PR 100 for the project"


def _write_block(idx: Path, bid: str, source_path: str, text: str, blocks: dict) -> None:
    blocks[bid] = {
        "source_path": source_path,
        "risk_class": "narrative",
        "must_keep": False,
        "raw_tokens": max(1, len(text) // 4),
    }
    (idx / "blocks" / f"{bid}.txt").write_text(text, encoding="utf-8")


@pytest.fixture()
def vault(tmp_path, monkeypatch):
    """A vault holding five same-shaped webapps plus one shared library."""
    monkeypatch.setenv("TOKENPAK_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("TOKENPAK_VAULT_CONFIG", str(tmp_path / "vault.yaml"))
    monkeypatch.delenv("TOKENPAK_PROJECT", raising=False)

    idx = tmp_path / "index"
    (idx / "blocks").mkdir(parents=True)
    blocks: dict[str, dict] = {}

    for name in PROJECTS:
        _write_block(
            idx,
            f"{name}-wb",
            f"{tmp_path}/workspace/{name}/notes/pr-100.md",
            f"PR 100 for the {name} web app. Audit checklist: auth refactor, "
            f"review the session handling, migration 100, rollout plan. Review "
            f"notes for pull request 100.",
            blocks,
        )
        _write_block(
            idx,
            f"{name}-st",
            f"{tmp_path}/staging/{name}/RELEASE.md",
            "Staging notes covering PR 100 review and the audit of the release "
            "for pull request 100. Deployment of the web app.",
            blocks,
        )
        _write_block(
            idx,
            f"{name}-ar",
            f"{tmp_path}/archive/{name}-legacy/pr-100.md",
            f"ARCHIVED stale copy. PR 100 audit review pull request 100 web app "
            f"{name} superseded content. PR 100 PR 100 audit audit review.",
            blocks,
        )

    _write_block(
        idx,
        "shared-runbook",
        f"{tmp_path}/shared/runbooks/pr-audit.md",
        "Shared runbook: how to audit any PR. Applies to every project. Review "
        "checklist for pull request handling.",
        blocks,
    )
    (idx / "index.json").write_text(json.dumps({"blocks": blocks}), encoding="utf-8")

    roots = "\n".join(
        f"""  - id: {name}
    aliases: [{name.split("-")[0]}]
    roots:
      - path: {tmp_path}/workspace/{name}
        role: workbench
      - path: {tmp_path}/staging/{name}
        role: staging
      - path: {tmp_path}/archive/{name}-legacy
        role: archive"""
        for name in PROJECTS
    )
    (tmp_path / "vault.yaml").write_text(
        f"version: 1\npaths: []\nprojects:\n{roots}\n"
        f"  - id: shared-runbooks\n    roots:\n"
        f"      - path: {tmp_path}/shared\n        role: library\n        shared: true\n",
        encoding="utf-8",
    )

    backend = SQLiteRetrievalBackend(str(idx), registry=vault_config.load().registry())
    backend.maybe_reload()
    return backend, tmp_path


def _paths(results, tmp_path) -> list[str]:
    return [b["source_path"].replace(str(tmp_path), "") for b, _ in results]


# ---------------------------------------------------------------------------
# The collision itself
# ---------------------------------------------------------------------------


def test_unscoped_search_spans_multiple_projects(vault):
    """Baseline: lexical ranking alone cannot separate the projects."""
    backend, tmp_path = vault
    results = backend.search(COLLIDING_QUERY, top_k=5, min_score=0.0, exclude_roles=[])
    spanned = backend._projects_spanned([b["block_id"] for b, _ in results])
    assert len(spanned) > 1, "fixture should reproduce a genuine cross-project collision"


def test_ambiguous_scope_fails_closed(vault):
    """Unresolvable scope returns nothing rather than a blend."""
    backend, _ = vault
    scoped = backend.search_scoped(COLLIDING_QUERY, top_k=5, min_score=0.0)
    assert scoped.suppressed
    assert scoped.results == []
    assert len(scoped.spanned) > 1
    # The candidate list is what lets a caller ask a better question.
    assert all(p in PROJECTS for p in scoped.spanned)


def test_auto_injection_fails_closed(vault):
    """The automatic path must never silently inject a cross-project blend."""
    backend, _ = vault
    text, tokens, refs = backend.compile_injection(COLLIDING_QUERY, min_score=0.0)
    assert (text, tokens, refs) == ("", 0, [])


# ---------------------------------------------------------------------------
# Scope resolution
# ---------------------------------------------------------------------------


def test_explicit_scope_excludes_every_other_project(vault):
    backend, tmp_path = vault
    scoped = backend.search_scoped(
        COLLIDING_QUERY, top_k=10, min_score=0.0, project="corvid-shop"
    )
    assert scoped.results
    for path in _paths(scoped.results, tmp_path):
        assert "corvid-shop" in path or "/shared/" in path


def test_scope_spans_all_roots_of_one_project(vault):
    """Workbench and staging are one project, resolved from either directory."""
    backend, tmp_path = vault
    scoped = backend.search_scoped(
        COLLIDING_QUERY,
        top_k=10,
        min_score=0.0,
        cwd=f"{tmp_path}/staging/delta-market/some/nested/dir",
    )
    assert scoped.project_id == "delta-market"
    kinds = {p.split("/")[1] for p in _paths(scoped.results, tmp_path)}
    assert {"workspace", "staging"} <= kinds


def test_alias_named_in_query_resolves_scope(vault):
    backend, _ = vault
    scoped = backend.search_scoped("audit PR 100 for bluefin", top_k=5, min_score=0.0)
    assert scoped.project_id == "bluefin-portal"
    assert scoped.scope is not None and scoped.scope.source == "query"


def test_alias_matching_is_word_boundary_anchored(vault):
    """A substring of a longer word must not resolve scope."""
    backend, _ = vault
    assert backend.registry.projects_named_in("bluefinch migration") == ()
    assert backend.registry.projects_named_in("the bluefin app") == ("bluefin-portal",)


def test_two_projects_named_in_query_is_not_a_guess(vault):
    backend, _ = vault
    scoped = backend.search_scoped("compare acme and corvid PR 100", top_k=5, min_score=0.0)
    assert scoped.project_id is None
    assert scoped.scope is not None and scoped.scope.source == "query_ambiguous"


def test_cwd_and_query_naming_different_projects_is_a_conflict(vault, tmp_path):
    """Working directory must not silently override a project named in the query.

    Letting cwd win yields the worst outcome available: a coherent, confident
    answer about the project the caller did not ask for, with no blend to
    notice and no candidates reported.
    """
    backend, _ = vault
    scoped = backend.search_scoped(
        "audit PR 100 for bluefin",
        top_k=5,
        min_score=0.0,
        cwd=f"{tmp_path}/workspace/acme-storefront",
    )
    assert scoped.project_id is None
    assert scoped.scope is not None and scoped.scope.source == "cwd_query_conflict"
    assert set(scoped.scope.candidates) == {"acme-storefront", "bluefin-portal"}

    # Agreement between the two signals is still a clean resolution.
    agreeing = backend.search_scoped(
        "audit PR 100 for bluefin",
        top_k=5,
        min_score=0.0,
        cwd=f"{tmp_path}/workspace/bluefin-portal",
    )
    assert agreeing.project_id == "bluefin-portal"


def test_contention_is_measured_over_the_corpus_not_the_page(vault):
    """A competing project below the caller's page must still be detected.

    Bounding the probe to a fixed window only shrinks the range in which a
    rival project can hide; it never closes it.
    """
    backend, _ = vault
    for page in (1, 2, 3, 5, 10):
        scoped = backend.search_scoped(COLLIDING_QUERY, top_k=page, min_score=0.0)
        assert scoped.suppressed, f"contention missed at top_k={page}"


def test_env_pin_resolves_scope(vault, monkeypatch):
    backend, tmp_path = vault
    monkeypatch.setenv("TOKENPAK_PROJECT", "echo-cart")
    scoped = backend.search_scoped(COLLIDING_QUERY, top_k=10, min_score=0.0)
    assert scoped.project_id == "echo-cart"
    for path in _paths(scoped.results, tmp_path):
        assert "echo-cart" in path or "/shared/" in path


def test_unknown_project_is_rejected(vault):
    backend, _ = vault
    with pytest.raises(ScopeConflictError):
        backend.search_scoped(COLLIDING_QUERY, project="not-a-project")


# ---------------------------------------------------------------------------
# Overlap, sharing, and roles
# ---------------------------------------------------------------------------


def test_shared_resource_visible_from_every_scope(vault):
    backend, tmp_path = vault
    for name in ("acme-storefront", "echo-cart"):
        scoped = backend.search_scoped(
            "shared runbook audit checklist", top_k=10, min_score=0.0, project=name
        )
        assert any("/shared/" in p for p in _paths(scoped.results, tmp_path))


def test_shared_blocks_do_not_create_ambiguity(vault):
    """A block everyone can see is not evidence two projects are in contention."""
    backend, _ = vault
    scoped = backend.search_scoped(COLLIDING_QUERY, top_k=5, min_score=0.0)
    assert "shared-runbooks" not in scoped.spanned


def test_shared_library_cannot_win_dominance(vault):
    backend, _ = vault
    scoped = backend.search_scoped(
        COLLIDING_QUERY, top_k=5, min_score=0.0, on_ambiguous=AmbiguityPolicy.DOMINANT
    )
    assert scoped.project_id in PROJECTS


def test_archive_role_excluded_by_default(vault):
    """An archived copy is still that project, but must not outrank the live tree."""
    backend, tmp_path = vault
    scoped = backend.search_scoped(
        COLLIDING_QUERY, top_k=10, min_score=0.0, project="acme-storefront"
    )
    assert not any("/archive/" in p for p in _paths(scoped.results, tmp_path))
    # Still addressable when explicitly asked for.
    included = backend.search(
        COLLIDING_QUERY, top_k=10, min_score=0.0, project="acme-storefront", exclude_roles=[]
    )
    assert any("/archive/" in p for p in _paths(included, tmp_path))


def test_longest_prefix_wins_for_nested_roots(tmp_path):
    """A nested root outranks a broader one regardless of declaration order."""
    registry = ProjectRegistry.from_config(
        [
            {"id": "outer", "roots": [{"path": f"{tmp_path}/vault", "role": "notes"}]},
            {
                "id": "inner",
                "roots": [{"path": f"{tmp_path}/vault/projects/inner", "role": "notes"}],
            },
        ]
    )
    assert registry.resolve_path(f"{tmp_path}/vault/projects/inner/a.md").project_ids == ("inner",)
    assert registry.resolve_path(f"{tmp_path}/vault/other/a.md").project_ids == ("outer",)


def test_sibling_prefix_does_not_match(tmp_path):
    """``/srv/foo`` must not match root ``/srv/fo`` the way a string prefix would."""
    registry = ProjectRegistry.from_config(
        [{"id": "fo", "roots": [{"path": f"{tmp_path}/fo"}]}]
    )
    assert registry.resolve_path(f"{tmp_path}/foo/a.md").project_ids == ()
    assert registry.resolve_path(f"{tmp_path}/fo/a.md").project_ids == ("fo",)


def test_multi_project_root_is_declared_once(tmp_path):
    registry = ProjectRegistry.from_config(
        [
            {"id": "a", "roots": [{"path": f"{tmp_path}/shared", "projects": ["a", "b"]}]},
            {"id": "b", "roots": []},
        ]
    )
    assert set(registry.resolve_path(f"{tmp_path}/shared/x.md").project_ids) == {"a", "b"}


def test_shared_root_carries_the_sentinel(tmp_path):
    registry = ProjectRegistry.from_config(
        [{"id": "lib", "roots": [{"path": f"{tmp_path}/lib", "shared": True}]}]
    )
    assert SHARED in registry.resolve_path(f"{tmp_path}/lib/x.md").project_ids


# ---------------------------------------------------------------------------
# Registry integrity — ambiguity is a load-time error, not a silent tiebreak
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "declaration",
    [
        pytest.param(
            [{"id": "a", "roots": [{"path": "/x"}]}, {"id": "b", "roots": [{"path": "/x"}]}],
            id="same-root-two-projects",
        ),
        pytest.param(
            [
                {
                    "id": "a",
                    "roots": [
                        {"path": "/x", "role": "archive"},
                        {"path": "/x", "role": "workbench"},
                    ],
                }
            ],
            id="same-root-conflicting-roles",
        ),
        pytest.param(
            [{"id": "a", "roots": [{"path": "/x", "shared": "false"}]}],
            id="shared-must-be-boolean",
        ),
        pytest.param([{"id": 123, "roots": []}], id="project-id-must-be-string"),
        pytest.param(
            [{"id": "a", "aliases": "", "roots": []}],
            id="empty-alias-is-not-a-query-wildcard",
        ),
        pytest.param(
            [{"id": "a", "aliases": [123], "roots": []}],
            id="alias-must-be-string",
        ),
        pytest.param(
            [{"id": "a", "roots": [{"path": 123}]}],
            id="root-path-must-be-string",
        ),
        pytest.param(
            [{"id": "a", "roots": [{"path": "/x", "role": ["archive"]}]}],
            id="root-role-must-be-string",
        ),
        pytest.param(
            [{"id": "a", "roots": [{"path": "/x", "projects": [123]}]}],
            id="root-project-membership-must-be-string",
        ),
        pytest.param(
            [{"id": "a", "roots": [{"path": "/x", "projects": ["a", "ghost"]}]}],
            id="undeclared-project-reference",
        ),
        pytest.param([{"id": "*", "roots": []}], id="reserved-sentinel-as-id"),
        pytest.param(
            [{"id": "a", "roots": []}, {"id": "a", "roots": []}], id="duplicate-id"
        ),
        pytest.param([{"id": "Bad Id!", "roots": []}], id="malformed-id"),
        pytest.param([{"roots": []}], id="missing-id"),
        pytest.param([{"id": "a", "roots": [{"role": "workbench"}]}], id="root-missing-path"),
    ],
)
def test_ambiguous_or_malformed_registry_is_rejected(declaration):
    with pytest.raises(ScopeConflictError):
        ProjectRegistry.from_config(declaration)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def test_registry_edit_re_resolves_without_index_change(vault):
    """Membership follows the registry, not just index.json's mtime."""
    backend, tmp_path = vault
    narrowed = ProjectRegistry.from_config(
        [
            {
                "id": "acme-storefront",
                "roots": [{"path": f"{tmp_path}/workspace/acme-storefront", "role": "workbench"}],
            }
        ]
    )
    backend.set_registry(narrowed)
    backend.maybe_reload()

    scoped = backend.search_scoped(
        COLLIDING_QUERY, top_k=10, min_score=0.0, project="acme-storefront"
    )
    assert scoped.results
    for path in _paths(scoped.results, tmp_path):
        assert "workspace/acme-storefront" in path


def test_default_json_blocks_backend_enforces_scope(vault, tmp_path, monkeypatch):
    """The *default* retrieval backend must enforce the guarantee too.

    `RETRIEVAL_BACKEND` defaults to `json_blocks`, so a guarantee implemented
    only in the SQLite backend would be false on a default install — and
    silently so. Covers the same contract against the in-memory index.
    """
    import tokenpak.proxy.vault_bridge as vault_bridge

    monkeypatch.setattr(vault_bridge, "_PROJECT_REGISTRY_STATE", None, raising=False)

    # This backend drops query terms appearing in >40% of the corpus as
    # non-selective. A tiny fixture makes the colliding terms *look* common and
    # gates them out entirely, which no real vault would do — pad the corpus so
    # `pr`/`100` are as selective here as they are in a real index.
    idx = tmp_path / "index"
    existing = json.loads((idx / "index.json").read_text(encoding="utf-8"))["blocks"]
    for n in range(60):
        _write_block(
            idx,
            f"filler-{n}",
            f"{tmp_path}/unclaimed/doc-{n}.md",
            f"unrelated topic {n} " + " ".join(f"lorem{n}_{w}" for w in range(40)),
            existing,
        )
    (idx / "index.json").write_text(json.dumps({"blocks": existing}), encoding="utf-8")

    index = vault_bridge.VaultIndex(str(idx))
    index.maybe_reload()

    # Unresolved scope spanning several projects fails closed.
    unresolved = index.search_scoped(COLLIDING_QUERY, top_k=5, min_score=0.0)
    assert unresolved.suppressed and not unresolved.results
    assert len(unresolved.spanned) > 1

    # A small page must not make contention invisible (the probe window).
    assert index.search_scoped(COLLIDING_QUERY, top_k=1, min_score=0.0).suppressed

    # An explicit scope returns only that project (plus shared resources).
    scoped = index.search_scoped(
        COLLIDING_QUERY, top_k=10, min_score=0.0, project="corvid-shop"
    )
    assert scoped.results
    for block, _ in scoped.results:
        path = str(block.get("source_path", "")).replace(str(tmp_path), "")
        assert "corvid-shop" in path or "/shared/" in path

    # Filtering happens before truncation, so a scoped page is not starved by
    # higher-scoring out-of-scope blocks.
    assert len(scoped.results) >= 2

    # The automatic path is the one users cannot inspect, and on a default
    # install it is what actually runs — testing `search_scoped` alone proves
    # the primitive works, not that the guarantee holds.
    assert index.compile_injection(COLLIDING_QUERY, min_score=0.0) == ("", 0, [])

    injected, tokens, refs = index.compile_injection(
        COLLIDING_QUERY, min_score=0.0, project="corvid-shop"
    )
    assert refs, "a resolved scope must still inject"
    for ref in refs:
        assert "corvid-shop" in ref or "/shared/" in ref

    # A bad scope pin costs the injection, not the request.
    monkeypatch.setenv("TOKENPAK_PROJECT", "typoed-project")
    assert index.compile_injection(COLLIDING_QUERY, min_score=0.0) == ("", 0, [])


def test_explicit_scope_on_inactive_registry_raises(tmp_path, monkeypatch):
    """An explicit scope must bind or fail — never silently return unscoped.

    A vault.yaml typo would otherwise convert every explicitly-scoped call in
    the system into an unscoped blend, with nothing to signal it.
    """
    monkeypatch.setenv("TOKENPAK_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("TOKENPAK_VAULT_CONFIG", str(tmp_path / "vault.yaml"))
    monkeypatch.delenv("TOKENPAK_PROJECT", raising=False)

    idx = tmp_path / "index"
    (idx / "blocks").mkdir(parents=True)
    blocks: dict[str, dict] = {}
    _write_block(idx, "b0", f"{tmp_path}/anywhere/0.md", "audit PR 100 review", blocks)
    (idx / "index.json").write_text(json.dumps({"blocks": blocks}), encoding="utf-8")

    backend = SQLiteRetrievalBackend(str(idx), registry=ProjectRegistry())
    backend.maybe_reload()

    with pytest.raises(ScopeConflictError):
        backend.search_scoped(COLLIDING_QUERY, project="acme-storefront")

    # Unscoped callers are unaffected.
    assert not backend.search_scoped(COLLIDING_QUERY, min_score=0.0).suppressed


def test_bad_env_pin_does_not_raise_through_injection(vault, monkeypatch):
    """The automatic path must fail closed, not raise into the request pipeline.

    `compile_injection` runs inside proxied requests; a typo in $TOKENPAK_PROJECT
    must cost the injection, not the request.
    """
    backend, _ = vault
    monkeypatch.setenv("TOKENPAK_PROJECT", "typoed-project-name")

    # The library call still surfaces the error to a direct caller...
    with pytest.raises(ScopeConflictError):
        backend.search_scoped(COLLIDING_QUERY, min_score=0.0)

    # ...but the injection path swallows it and contributes nothing.
    assert backend.compile_injection(COLLIDING_QUERY, min_score=0.0) == ("", 0, [])


def test_broken_registry_fails_closed_and_keeps_last_good(vault, tmp_path, monkeypatch):
    """A present-but-unreadable declaration must not degrade to "no scoping".

    It fires exactly when someone is switching scoping on, so failing open would
    restore the blend at the worst possible moment — and re-resolving against an
    empty registry would delete the only correct membership data still held.
    """
    backend, _ = vault

    def membership_rows() -> set[tuple[str, str]]:
        conn = backend._connect()
        try:
            return {
                (str(a), str(b))
                for a, b in conn.execute(
                    "SELECT block_id, project_id FROM block_projects"
                ).fetchall()
            }
        finally:
            conn.close()

    before = membership_rows()
    assert before, "fixture should have membership recorded"

    # Corrupt the declaration and reload the registry the way production does.
    (tmp_path / "vault.yaml").write_text(
        "version: 1\nprojects:\n  - id: a\n    roots:\n      - path: /x\n"
        "  - id: b\n    roots:\n      - path: /x\n",  # same root, two projects
        encoding="utf-8",
    )
    from tokenpak.vault import sqlite_backend as _sb

    registry, error = _sb._load_registry()
    assert error is not None, "an ambiguous declaration must be reported, not swallowed"
    backend._registry, backend._registry_error = registry, error

    # Scoped queries are refused rather than answered unscoped.
    with pytest.raises(ScopeConflictError):
        backend.search_scoped(COLLIDING_QUERY, min_score=0.0)
    with pytest.raises(ScopeConflictError):
        backend.search_scoped(COLLIDING_QUERY, min_score=0.0, project="acme-storefront")

    # Auto-injection contributes nothing instead of blending.
    assert backend.compile_injection(COLLIDING_QUERY, min_score=0.0) == ("", 0, [])

    # And the last-good membership rows survive the failed load — a reload must
    # not re-resolve against the empty registry and delete them.
    backend.maybe_reload()
    assert membership_rows() == before


def test_no_registry_behaves_exactly_as_before(tmp_path, monkeypatch):
    """A vault with no projects declared is unscoped and unfiltered."""
    monkeypatch.setenv("TOKENPAK_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("TOKENPAK_VAULT_CONFIG", str(tmp_path / "vault.yaml"))
    monkeypatch.delenv("TOKENPAK_PROJECT", raising=False)

    idx = tmp_path / "index"
    (idx / "blocks").mkdir(parents=True)
    blocks: dict[str, dict] = {}
    for i in range(3):
        _write_block(idx, f"b{i}", f"{tmp_path}/anywhere/{i}.md", "audit PR 100 review", blocks)
    (idx / "index.json").write_text(json.dumps({"blocks": blocks}), encoding="utf-8")

    backend = SQLiteRetrievalBackend(str(idx), registry=ProjectRegistry())
    backend.maybe_reload()

    scoped = backend.search_scoped(COLLIDING_QUERY, top_k=5, min_score=0.0)
    assert not scoped.suppressed
    assert len(scoped.results) == len(backend.search(COLLIDING_QUERY, top_k=5, min_score=0.0))


def test_config_without_projects_key_loads(tmp_path, monkeypatch):
    """The registry is additive within schema v1 — old configs keep working."""
    monkeypatch.setenv("TOKENPAK_VAULT_CONFIG", str(tmp_path / "vault.yaml"))
    (tmp_path / "vault.yaml").write_text("version: 1\npaths: []\n", encoding="utf-8")
    cfg = vault_config.load()
    assert cfg.projects == []
    assert not cfg.registry().active
    # And round-trips without gaining an empty key.
    assert "projects" not in cfg.to_dict()
