# SPDX-License-Identifier: Apache-2.0
"""Implementation-family and production-entry-point project-scope conformance."""

from __future__ import annotations

import importlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from tokenpak.vault.backend_protocol import RetrievalBackendBase
from tokenpak.vault.project_scope import (
    PROJECT_FILTERING_CAPABILITY,
    SCOPE_RESOLUTION_CAPABILITY,
    ProjectScopeImplementation,
    ScopeConflictError,
    discover_project_scope_implementations,
)

COLLIDING_QUERY = "audit the PR 100 for the project"
PROJECTS = ("acme", "bluefin")
SHIPPING_IMPLEMENTATIONS = discover_project_scope_implementations()


def _write_block(
    index_dir: Path,
    blocks: dict[str, dict[str, object]],
    block_id: str,
    source_path: str,
    content: str,
) -> None:
    blocks[block_id] = {
        "source_path": source_path,
        "risk_class": "narrative",
        "must_keep": False,
        "raw_tokens": max(1, len(content) // 4),
    }
    (index_dir / "blocks" / f"{block_id}.txt").write_text(content, encoding="utf-8")


@pytest.fixture()
def scoped_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Two colliding projects, a shared root, and enough filler for stable BM25."""
    index_dir = tmp_path / ".tokenpak"
    (index_dir / "blocks").mkdir(parents=True)
    blocks: dict[str, dict[str, object]] = {}
    for project in PROJECTS:
        _write_block(
            index_dir,
            blocks,
            f"{project}-pr100",
            str(tmp_path / "workspace" / project / "notes" / "pr-100.md"),
            f"Audit PR 100 for the {project} project. Review pull request 100.",
        )
    _write_block(
        index_dir,
        blocks,
        "shared-runbook",
        str(tmp_path / "shared" / "pr-audit.md"),
        "Shared pull request audit runbook and review checklist.",
    )
    for number in range(8):
        _write_block(
            index_dir,
            blocks,
            f"filler-{number}",
            str(tmp_path / "unclaimed" / f"topic-{number}.md"),
            " ".join(f"unrelated{number}_{word}" for word in range(30)),
        )
    (index_dir / "index.json").write_text(json.dumps({"blocks": blocks}), encoding="utf-8")

    config_path = tmp_path / "vault.yaml"
    config_path.write_text(
        "version: 1\n"
        "paths: []\n"
        "projects:\n"
        "  - id: acme\n"
        "    aliases: [acme-app]\n"
        "    roots:\n"
        f"      - path: {tmp_path}/workspace/acme\n"
        "        role: workbench\n"
        "  - id: bluefin\n"
        "    aliases: [bluefin-app]\n"
        "    roots:\n"
        f"      - path: {tmp_path}/workspace/bluefin\n"
        "        role: workbench\n"
        "  - id: shared-runbooks\n"
        "    roots:\n"
        f"      - path: {tmp_path}/shared\n"
        "        role: library\n"
        "        shared: true\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TOKENPAK_VAULT_CONFIG", str(config_path))
    monkeypatch.setenv("TOKENPAK_VAULT_ROOT", str(tmp_path))
    monkeypatch.delenv("TOKENPAK_PROJECT", raising=False)
    return index_dir, config_path


class MinimalScopedExtension(RetrievalBackendBase):
    """Minimal public-interface implementation used by the conformance matrix."""

    project_scope_capabilities = frozenset(
        {PROJECT_FILTERING_CAPABILITY, SCOPE_RESOLUTION_CAPABILITY}
    )

    def __init__(self, inner: Any):
        self.inner = inner

    @property
    def available(self) -> bool:
        return bool(self.inner.available)

    def maybe_reload(self) -> None:
        for timer in ("_last_loaded", "_last_checked"):
            if hasattr(self.inner, timer):
                setattr(self.inner, timer, 0)
        self.inner.maybe_reload()

    def search(self, query: str, top_k: int = 5, min_score: float = 2.0, **kwargs):
        return self.inner.search(query, top_k, min_score, **kwargs)

    def search_scoped(self, query: str, top_k: int = 5, min_score: float = 2.0, **kwargs):
        return self.inner.search_scoped(query, top_k, min_score, **kwargs)

    def compile_injection(
        self, query: str, budget: int = 4000, top_k: int = 5, min_score: float = 2.0, **kwargs
    ):
        return self.inner.compile_injection(query, budget, top_k, min_score, **kwargs)


@dataclass
class BackendCase:
    name: str
    backend: Any
    shipping: bool


def _new_shipping_backend(
    implementation: ProjectScopeImplementation,
    index_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    module = importlib.import_module(implementation.implementation_class.__module__)
    if hasattr(module, "_PROJECT_REGISTRY_STATE"):
        monkeypatch.setattr(module, "_PROJECT_REGISTRY_STATE", None)
    backend = implementation.implementation_class(str(index_dir))
    backend.maybe_reload()
    return backend


_MATRIX_PARAMS: tuple[object, ...] = (*SHIPPING_IMPLEMENTATIONS, "third_party")


@pytest.fixture(params=_MATRIX_PARAMS, ids=lambda item: getattr(item, "implementation_id", item))
def backend_case(request, scoped_vault, monkeypatch) -> BackendCase:
    index_dir, _ = scoped_vault
    if request.param == "third_party":
        sdk_spec = next(
            implementation
            for implementation in SHIPPING_IMPLEMENTATIONS
            if implementation.implementation_id == "sdk_plugin"
        )
        inner = _new_shipping_backend(sdk_spec, index_dir, monkeypatch)
        return BackendCase("third_party", MinimalScopedExtension(inner), False)
    implementation = request.param
    return BackendCase(
        implementation.implementation_id,
        _new_shipping_backend(implementation, index_dir, monkeypatch),
        True,
    )


def _paths(results: list[tuple[dict, float]]) -> list[str]:
    return [str(block.get("source_path", "")) for block, _ in results]


def test_discovery_matrix_has_unique_capable_shipping_implementations() -> None:
    assert SHIPPING_IMPLEMENTATIONS
    ids = [item.implementation_id for item in SHIPPING_IMPLEMENTATIONS]
    assert len(ids) == len(set(ids))
    for item in SHIPPING_IMPLEMENTATIONS:
        instance = item.implementation_class.__new__(item.implementation_class)
        assert instance.supports_project_scope
        assert instance.supports_scope_resolution


def test_ambiguous_query_fails_closed_across_matrix(backend_case: BackendCase) -> None:
    scoped = backend_case.backend.search_scoped(COLLIDING_QUERY, min_score=0.0)
    assert scoped.suppressed
    assert scoped.results == []
    assert set(scoped.spanned) == set(PROJECTS)


def test_explicit_scope_filters_before_top_k_across_matrix(backend_case: BackendCase) -> None:
    scoped = backend_case.backend.search_scoped(
        COLLIDING_QUERY, top_k=10, min_score=0.0, project="acme"
    )
    assert scoped.results
    assert all("/workspace/acme/" in path or "/shared/" in path for path in _paths(scoped.results))


def test_automatic_injection_fails_closed_across_matrix(backend_case: BackendCase) -> None:
    assert backend_case.backend.compile_injection(COLLIDING_QUERY, min_score=0.0) == ("", 0, [])
    _, _, refs = backend_case.backend.compile_injection(
        COLLIDING_QUERY, min_score=0.0, project="bluefin"
    )
    assert refs
    assert all("/workspace/bluefin/" in ref or "/shared/" in ref for ref in refs)


def test_invalid_project_is_typed_refusal_across_matrix(backend_case: BackendCase) -> None:
    with pytest.raises(ScopeConflictError):
        backend_case.backend.search_scoped(COLLIDING_QUERY, project="ghost")


def test_cwd_query_conflict_fails_closed_across_matrix(
    backend_case: BackendCase, tmp_path: Path
) -> None:
    scoped = backend_case.backend.search_scoped(
        "audit PR 100 for bluefin-app",
        min_score=0.0,
        cwd=str(tmp_path / "workspace" / "acme"),
    )
    assert scoped.suppressed
    assert scoped.scope is not None
    assert scoped.scope.source == "cwd_query_conflict"


def test_environment_scope_resolves_across_matrix(
    backend_case: BackendCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TOKENPAK_PROJECT", "bluefin")
    scoped = backend_case.backend.search_scoped(COLLIDING_QUERY, min_score=0.0)
    assert scoped.project_id == "bluefin"
    assert scoped.scope is not None and scoped.scope.source == "env"
    assert all(
        "/workspace/bluefin/" in path or "/shared/" in path for path in _paths(scoped.results)
    )


def test_unique_query_alias_resolves_across_matrix(backend_case: BackendCase) -> None:
    scoped = backend_case.backend.search_scoped("audit PR 100 for acme-app", min_score=0.0)
    assert scoped.project_id == "acme"
    assert scoped.scope is not None and scoped.scope.source == "query"


def _force_reload(backend: Any) -> None:
    for timer in ("_last_loaded", "_last_checked"):
        if hasattr(backend, timer):
            setattr(backend, timer, 0)
    backend.maybe_reload()


def test_valid_registry_edit_reloads_across_matrix(
    backend_case: BackendCase, scoped_vault: tuple[Path, Path]
) -> None:
    _, config_path = scoped_vault
    original = config_path.read_text(encoding="utf-8")
    config_path.write_text(
        original.replace("  - id: acme\n", "  - id: renamed-acme\n"),
        encoding="utf-8",
    )
    _force_reload(backend_case.backend)

    with pytest.raises(ScopeConflictError):
        backend_case.backend.search_scoped(COLLIDING_QUERY, project="acme")
    renamed = backend_case.backend.search_scoped(
        COLLIDING_QUERY, min_score=0.0, project="renamed-acme"
    )
    assert renamed.results
    assert all("/workspace/acme/" in path or "/shared/" in path for path in _paths(renamed.results))


def test_broken_then_fixed_registry_recovers_across_matrix(
    backend_case: BackendCase, scoped_vault: tuple[Path, Path], tmp_path: Path
) -> None:
    _, config_path = scoped_vault
    original = config_path.read_text(encoding="utf-8")
    broken = original.replace(
        f"      - path: {tmp_path}/workspace/bluefin\n",
        f"      - path: {tmp_path}/workspace/acme\n",
    )
    config_path.write_text(broken, encoding="utf-8")
    _force_reload(backend_case.backend)
    with pytest.raises(ScopeConflictError):
        backend_case.backend.search_scoped(COLLIDING_QUERY, min_score=0.0)

    config_path.write_text(original, encoding="utf-8")
    _force_reload(backend_case.backend)
    assert backend_case.backend.search_scoped(
        COLLIDING_QUERY, min_score=0.0, project="bluefin"
    ).results


@pytest.mark.parametrize("signal", ["explicit", "environment", "cwd"])
def test_any_scope_signal_without_registry_refuses_across_matrix(
    backend_case: BackendCase,
    scoped_vault: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    signal: str,
) -> None:
    _, config_path = scoped_vault
    config_path.write_text("version: 1\npaths: []\n", encoding="utf-8")
    _force_reload(backend_case.backend)

    kwargs: dict[str, str] = {}
    if signal == "explicit":
        kwargs["project"] = "acme"
    elif signal == "environment":
        monkeypatch.setenv("TOKENPAK_PROJECT", "acme")
    else:
        kwargs["cwd"] = str(config_path.parent / "workspace" / "acme")

    with pytest.raises(ScopeConflictError):
        backend_case.backend.search_scoped(COLLIDING_QUERY, **kwargs)


def test_public_pak_search_uses_scope_resolution_across_matrix(
    backend_case: BackendCase, scoped_vault: tuple[Path, Path]
) -> None:
    from tokenpak.vault.pak_adapter import search_as_paks

    # No scope signal over a colliding corpus fails closed rather than
    # returning a labelled-but-still-blended Pak list.
    assert search_as_paks(
        COLLIDING_QUERY,
        min_score=0.0,
        vault_index=backend_case.backend,
    ) == []

    scoped = search_as_paks(
        COLLIDING_QUERY,
        min_score=0.0,
        vault_index=backend_case.backend,
        project="acme",
    )
    assert scoped
    assert any(pak.scope.project == "acme" for pak in scoped)
    assert all(pak.scope.project != "bluefin" for pak in scoped)
    assert all("bluefin-pr100" not in pak.pak_id for pak in scoped)


def test_membership_read_failure_matrix(
    backend_case: BackendCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    if backend_case.name != "sqlite":
        pytest.skip("NOT APPLICABLE: only SQLite performs a second membership database read")
    backend = backend_case.backend
    original_connect = backend._connect
    calls = 0

    def fail_second_connect():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise sqlite3.OperationalError("simulated membership read failure")
        return original_connect()

    monkeypatch.setattr(backend, "_connect", fail_second_connect)
    with pytest.raises(ScopeConflictError, match="cannot verify project contention"):
        backend.search_scoped(COLLIDING_QUERY, min_score=0.0)


def test_sqlite_valid_registry_edit_blocks_until_membership_sync_commits(
    backend_case: BackendCase,
    scoped_vault: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if backend_case.name != "sqlite":
        pytest.skip("NOT APPLICABLE: only SQLite persists a membership join table")

    backend = backend_case.backend
    _, config_path = scoped_vault
    config_path.write_text(
        "version: 1\n"
        "paths: []\n"
        "projects:\n"
        "  - id: acme\n"
        "    roots:\n"
        f"      - path: {config_path.parent}/workspace/bluefin\n"
        "  - id: bluefin\n"
        "    roots:\n"
        f"      - path: {config_path.parent}/workspace/acme\n",
        encoding="utf-8",
    )

    original_connect = backend._connect
    calls = 0

    def fail_membership_sync():
        nonlocal calls
        calls += 1
        if calls == 2:  # checkpoint read succeeds; membership transaction fails
            raise sqlite3.OperationalError("simulated membership sync failure")
        return original_connect()

    monkeypatch.setattr(backend, "_connect", fail_membership_sync)
    _force_reload(backend)

    assert backend._registry_error is not None
    assert "membership synchronization failed" in backend._registry_error
    assert backend._registry.resolve_path(
        str(config_path.parent / "workspace" / "acme" / "notes" / "pr-100.md")
    ).project_ids == ("bluefin",)
    with pytest.raises(ScopeConflictError):
        backend.search_scoped(COLLIDING_QUERY, project="acme", min_score=0.0)
    with pytest.raises(ScopeConflictError):
        backend.search(COLLIDING_QUERY, project="acme", min_score=0.0)

    monkeypatch.setattr(backend, "_connect", original_connect)
    _force_reload(backend)
    acme_paths = _paths(
        backend.search_scoped(COLLIDING_QUERY, project="acme", min_score=0.0).results
    )
    assert all("/workspace/acme/" not in path for path in acme_paths)


def test_sdk_mcp_handlers_fail_closed_and_accept_explicit_scope(
    scoped_vault: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from tokenpak.sdk.integrations.claude_code import mcp_server
    from tokenpak.vault.retrieval import vault_index as sdk_index

    monkeypatch.setattr(sdk_index, "_PROJECT_REGISTRY_STATE", None)
    search = mcp_server._handle_search_corpus({"query": COLLIDING_QUERY, "top_k": 5})
    assert search["status"] == "ambiguous-project-scope"
    assert search["results"] == []
    assert set(search["candidates"]) == set(PROJECTS)

    related = mcp_server._handle_summarize_related_issues({"query": COLLIDING_QUERY, "top_k": 5})
    assert related["status"] == "ambiguous-project-scope"
    assert related["related"] == []

    explicit = mcp_server._handle_search_corpus(
        {"query": COLLIDING_QUERY, "top_k": 5, "project": "acme"}
    )
    assert explicit["status"] == "ok"
    assert explicit["results"]
    assert all("/workspace/acme/" in row["source_path"] for row in explicit["results"])

    invalid = mcp_server._handle_search_corpus({"query": COLLIDING_QUERY, "project": "ghost"})
    assert invalid["status"] == "invalid-project-scope"
    assert invalid["results"] == []


@pytest.mark.parametrize(
    ("handler_name", "query_key"),
    [
        ("_handle_build_context_pack", "query"),
        ("_handle_prepare_review_packet", "branch"),
    ],
)
def test_sdk_composite_handlers_enforce_scope_contract(
    scoped_vault: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    handler_name: str,
    query_key: str,
) -> None:
    from tokenpak.sdk.integrations.claude_code import mcp_server
    from tokenpak.vault.retrieval import vault_index as sdk_index

    _, config_path = scoped_vault
    handler = getattr(mcp_server, handler_name)
    monkeypatch.setattr(sdk_index, "_PROJECT_REGISTRY_STATE", None)

    ambiguous = handler({query_key: COLLIDING_QUERY})
    assert ambiguous["status"] == "ambiguous-project-scope"

    explicit = handler({query_key: COLLIDING_QUERY, "project": "acme"})
    assert explicit["status"] == "ok"
    assert explicit["corpus_hits"]
    assert all("/workspace/acme/" in row["source_path"] for row in explicit["corpus_hits"])

    invalid = handler({query_key: COLLIDING_QUERY, "project": "ghost"})
    assert invalid["status"] == "invalid-project-scope"

    config_path.write_text("version: 1\npaths: []\n", encoding="utf-8")
    monkeypatch.setattr(sdk_index, "_PROJECT_REGISTRY_STATE", None)
    no_registry = handler({query_key: COLLIDING_QUERY, "project": "acme"})
    assert no_registry["status"] == "invalid-project-scope"


def test_search_endpoint_fails_closed_across_matrix(
    backend_case: BackendCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tokenpak.proxy import app_endpoints, vault_bridge

    monkeypatch.setattr(vault_bridge, "get_vault_index", lambda: backend_case.backend)
    sent: list[tuple[int, dict[str, Any]]] = []
    monkeypatch.setattr(
        app_endpoints,
        "_send_json",
        lambda handler, status, payload: sent.append((status, payload)),
    )
    monkeypatch.setattr(
        app_endpoints,
        "_send_error",
        lambda *args, **kwargs: pytest.fail("capable backend was refused"),
    )

    app_endpoints._handle_vault_search(object(), {"q": [COLLIDING_QUERY]})
    assert sent[0][0] == 409
    assert sent[0][1]["error"] == "ambiguous_project_scope"
    assert sent[0][1]["results"] == []


def test_search_endpoint_does_not_overread_filtering_capability(
    scoped_vault: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from tokenpak.proxy import app_endpoints, vault_bridge
    from tokenpak.vault.project_scope import ProjectScopeCapabilities

    class FilteringOnlyBackend(ProjectScopeCapabilities):
        project_scope_capabilities = frozenset({PROJECT_FILTERING_CAPABILITY})
        available = True

        def search(self, *args, **kwargs):
            pytest.fail("an active registry must not fall through to unverified search")

    monkeypatch.setattr(vault_bridge, "get_vault_index", lambda: FilteringOnlyBackend())
    sent: list[tuple[int, str]] = []
    monkeypatch.setattr(
        app_endpoints,
        "_send_error",
        lambda handler, status, code, detail="": sent.append((status, code)),
    )
    monkeypatch.setattr(
        app_endpoints,
        "_send_json",
        lambda *args, **kwargs: pytest.fail("endpoint served an unverified result"),
    )

    app_endpoints._handle_vault_search(object(), {"q": [COLLIDING_QUERY]})
    assert sent == [(501, "scoping_unsupported")]


def test_companion_forwards_cwd_on_direct_block_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    from tokenpak.companion.mcp import tools

    calls: list[tuple[str, dict[str, Any] | None]] = []

    def fake_get(path: str, params: dict[str, Any] | None = None):
        calls.append((path, params))
        return 200, {"block_id": "bluefin-pr100", "content": "bluefin"}

    monkeypatch.setattr(tools, "_proxy_get", fake_get)
    tools._handle_vault_retrieve(
        None,
        {"block_id": "bluefin-pr100", "cwd": "/workspace/acme"},
    )
    assert calls == [("/tpk/v1/vault/block/bluefin-pr100", {"cwd": "/workspace/acme"})]


def test_companion_forwards_cwd_on_path_resolve_and_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tokenpak.companion.mcp import tools

    calls: list[tuple[str, dict[str, Any] | None]] = []

    def fake_get(path: str, params: dict[str, Any] | None = None):
        calls.append((path, params))
        if path == "/tpk/v1/vault/search":
            return 200, {"results": [{"block_id": "acme-pr100"}]}
        return 200, {"block_id": "acme-pr100", "content": "acme"}

    monkeypatch.setattr(tools, "_proxy_get", fake_get)
    tools._handle_vault_retrieve(
        None,
        {"path": "notes/pr-100.md", "cwd": "/workspace/acme"},
    )
    assert calls == [
        (
            "/tpk/v1/vault/search",
            {"q": "notes/pr-100.md", "limit": 1, "cwd": "/workspace/acme"},
        ),
        ("/tpk/v1/vault/block/acme-pr100", {"cwd": "/workspace/acme"}),
    ]


def test_block_endpoint_refuses_explicit_scope_without_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tokenpak.proxy import app_endpoints, vault_bridge

    config_path = tmp_path / "empty-vault.yaml"
    config_path.write_text("version: 1\npaths: []\n", encoding="utf-8")
    monkeypatch.setenv("TOKENPAK_VAULT_CONFIG", str(config_path))
    block_id = "acme-pr100"
    index = type(
        "BlockIndex",
        (),
        {
            "available": True,
            "blocks": {
                block_id: {
                    "block_id": block_id,
                    "source_path": str(tmp_path / "workspace" / "acme" / "pr-100.md"),
                    "raw_tokens": 4,
                }
            },
            "tokenpak_dir": str(tmp_path / ".tokenpak"),
        },
    )()
    monkeypatch.setattr(vault_bridge, "get_vault_index", lambda: index)
    sent: list[tuple[int, str]] = []
    monkeypatch.setattr(
        app_endpoints,
        "_send_error",
        lambda handler, status, code, detail="": sent.append((status, code)),
    )
    monkeypatch.setattr(
        app_endpoints,
        "_send_json",
        lambda *args, **kwargs: pytest.fail("endpoint served content despite unusable scope"),
    )

    app_endpoints._handle_vault_block(object(), block_id, {"project": ["acme"]})
    assert sent == [(501, "scoping_unavailable")]


@pytest.mark.parametrize("signal", ["cwd", "environment"])
def test_block_endpoint_fails_closed_when_registry_lookup_raises_for_implicit_scope(
    scoped_vault: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    signal: str,
) -> None:
    from tokenpak.proxy import app_endpoints, vault_bridge
    from tokenpak.vault import sqlite_backend

    block_id = "bluefin-pr100"
    index = type(
        "BlockIndex",
        (),
        {
            "available": True,
            "blocks": {
                block_id: {
                    "block_id": block_id,
                    "source_path": str(tmp_path / "workspace" / "bluefin" / "pr-100.md"),
                    "raw_tokens": 4,
                }
            },
            "tokenpak_dir": str(tmp_path / ".tokenpak"),
        },
    )()
    monkeypatch.setattr(vault_bridge, "get_vault_index", lambda: index)
    monkeypatch.setattr(
        sqlite_backend,
        "_load_registry",
        lambda: (_ for _ in ()).throw(OSError("simulated registry read failure")),
    )
    query: dict[str, list[str]] = {}
    if signal == "cwd":
        query["cwd"] = [str(tmp_path / "workspace" / "acme")]
    else:
        monkeypatch.setenv("TOKENPAK_PROJECT", "acme")

    sent: list[tuple[int, str]] = []
    monkeypatch.setattr(
        app_endpoints,
        "_send_error",
        lambda handler, status, code, detail="": sent.append((status, code)),
    )
    monkeypatch.setattr(
        app_endpoints,
        "_send_json",
        lambda *args, **kwargs: pytest.fail("endpoint served content after scope lookup failed"),
    )

    app_endpoints._handle_vault_block(object(), block_id, query)
    assert sent == [(503, "project_scope_unavailable")]


def test_block_endpoint_enforces_cwd_scope(
    scoped_vault: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tokenpak.proxy import app_endpoints, vault_bridge

    block_id = "bluefin-pr100"
    index = type(
        "BlockIndex",
        (),
        {
            "available": True,
            "blocks": {
                block_id: {
                    "block_id": block_id,
                    "source_path": str(tmp_path / "workspace" / "bluefin" / "notes" / "pr-100.md"),
                    "raw_tokens": 4,
                }
            },
            "tokenpak_dir": str(tmp_path / ".tokenpak"),
        },
    )()
    monkeypatch.setattr(vault_bridge, "get_vault_index", lambda: index)
    sent: list[tuple[int, str]] = []
    monkeypatch.setattr(
        app_endpoints,
        "_send_error",
        lambda handler, status, code, detail="": sent.append((status, code)),
    )
    monkeypatch.setattr(
        app_endpoints,
        "_send_json",
        lambda *args, **kwargs: pytest.fail("endpoint served a block outside cwd scope"),
    )

    app_endpoints._handle_vault_block(
        object(),
        block_id,
        {"cwd": [str(tmp_path / "workspace" / "acme")]},
    )
    assert sent == [(404, "block_not_in_project")]


def test_pak_inspect_refuses_explicit_scope_without_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tokenpak.proxy import app_endpoints, vault_bridge

    config_path = tmp_path / "empty-vault.yaml"
    config_path.write_text("version: 1\npaths: []\n", encoding="utf-8")
    monkeypatch.setenv("TOKENPAK_VAULT_CONFIG", str(config_path))
    monkeypatch.setattr(vault_bridge, "get_vault_index", lambda: pytest.fail("must refuse first"))
    sent: list[tuple[int, str]] = []
    monkeypatch.setattr(
        app_endpoints,
        "_send_error",
        lambda handler, status, code, detail="": sent.append((status, code)),
    )
    monkeypatch.setattr(
        app_endpoints,
        "_send_json",
        lambda *args, **kwargs: pytest.fail("Pak inspect served an unverifiable scope"),
    )

    app_endpoints._handle_pak_inspect(object(), "vault:acme-pr100", {"project": ["acme"]})
    assert sent == [(501, "scoping_unavailable")]


def test_pak_inspect_authorizes_against_current_registry_after_valid_edit(
    scoped_vault: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tokenpak.proxy import app_endpoints, vault_bridge
    from tokenpak.vault import pak_adapter

    _, config_path = scoped_vault
    block_id = "target#12345678"
    block = {
        "block_id": block_id,
        "source_path": str(tmp_path / "workspace" / "acme" / "secret.md"),
        "raw_tokens": 4,
    }
    assert pak_adapter.vault_block_to_pak(block).scope.project == "acme"

    config_path.write_text(
        "version: 1\n"
        "paths: []\n"
        "projects:\n"
        "  - id: acme\n"
        "    roots:\n"
        f"      - path: {tmp_path}/workspace/retired-acme\n"
        "  - id: bluefin\n"
        "    roots:\n"
        f"      - path: {tmp_path}/workspace/acme\n",
        encoding="utf-8",
    )
    index = type("BlockIndex", (), {"blocks": {block_id: block}})()
    monkeypatch.setattr(vault_bridge, "get_vault_index", lambda: index)
    responses: list[tuple[str, int, object]] = []
    monkeypatch.setattr(
        app_endpoints,
        "_send_error",
        lambda handler, status, code, detail="": responses.append(("error", status, code)),
    )
    monkeypatch.setattr(
        app_endpoints,
        "_send_json",
        lambda handler, status, body: responses.append(("json", status, body)),
    )

    app_endpoints._handle_pak_inspect(
        object(), f"vault:{block_id}", {"project": ["acme"]}
    )
    assert responses == [("error", 404, "block_not_in_project")]

    responses.clear()
    app_endpoints._handle_pak_inspect(
        object(), f"vault:{block_id}", {"project": ["bluefin"]}
    )
    assert responses[0][0:2] == ("json", 200)
    assert isinstance(responses[0][2], dict)
    assert responses[0][2]["scope"]["project"] == "bluefin"


@pytest.mark.parametrize("membership", ["shared", "multi_project"])
def test_pak_inspect_honors_full_registry_membership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, membership: str
) -> None:
    from tokenpak.proxy import app_endpoints, vault_bridge

    source_root = tmp_path / "library"
    config_path = tmp_path / "vault.yaml"
    common = (
        "version: 1\n"
        "paths: []\n"
        "projects:\n"
        "  - id: acme\n"
        "    roots: []\n"
        "  - id: bluefin\n"
        "    roots: []\n"
    )
    if membership == "shared":
        declaration = (
            common
            + "  - id: library\n"
            + "    roots:\n"
            + f"      - path: {source_root}\n"
            + "        shared: true\n"
        )
    else:
        declaration = (
            "version: 1\n"
            "paths: []\n"
            "projects:\n"
            "  - id: acme\n"
            "    roots:\n"
            f"      - path: {source_root}\n"
            "        projects: [acme, bluefin]\n"
            "  - id: bluefin\n"
            "    roots: []\n"
        )
    config_path.write_text(declaration, encoding="utf-8")
    monkeypatch.setenv("TOKENPAK_VAULT_CONFIG", str(config_path))

    block_id = "library#12345678"
    block = {
        "block_id": block_id,
        "source_path": str(source_root / "guide.md"),
        "raw_tokens": 4,
    }
    monkeypatch.setattr(
        vault_bridge,
        "get_vault_index",
        lambda: type("BlockIndex", (), {"blocks": {block_id: block}})(),
    )
    responses: list[tuple[str, int, object]] = []
    monkeypatch.setattr(
        app_endpoints,
        "_send_error",
        lambda handler, status, code, detail="": responses.append(("error", status, code)),
    )
    monkeypatch.setattr(
        app_endpoints,
        "_send_json",
        lambda handler, status, body: responses.append(("json", status, body)),
    )

    for project in ("acme", "bluefin"):
        responses.clear()
        app_endpoints._handle_pak_inspect(
            object(), f"vault:{block_id}", {"project": [project]}
        )
        assert responses[0][0:2] == ("json", 200)


def test_pak_emission_uses_declared_registry(
    scoped_vault: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tokenpak.vault import pak_adapter

    _, config_path = scoped_vault
    pak = pak_adapter.vault_block_to_pak(
        {
            "block_id": "acme-pr100#12345678",
            "source_path": str(tmp_path / "workspace" / "acme" / "pr-100.md"),
            "raw_tokens": 10,
        }
    )
    assert pak.scope.project == "acme"

    unclaimed = pak_adapter.vault_block_to_pak(
        {
            "block_id": "other#12345678",
            "source_path": str(tmp_path / "workspace" / "not-declared" / "notes.md"),
            "raw_tokens": 10,
        }
    )
    assert unclaimed.scope.project is None

    config_path.write_text(
        "version: 1\n"
        "paths: []\n"
        "projects:\n"
        "  - id: acme\n"
        "    roots:\n"
        f"      - path: {tmp_path}/workspace/retired-acme\n"
        "  - id: bluefin\n"
        "    roots:\n"
        f"      - path: {tmp_path}/workspace/acme\n",
        encoding="utf-8",
    )
    refreshed = pak_adapter.vault_block_to_pak(
        {
            "block_id": "acme-pr100#12345678",
            "source_path": str(tmp_path / "workspace" / "acme" / "pr-100.md"),
            "raw_tokens": 10,
        }
    )
    assert refreshed.scope.project == "bluefin"
