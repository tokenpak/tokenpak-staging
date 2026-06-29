# SPDX-License-Identifier: Apache-2.0
"""MCP vault_retrieve adapter coverage."""

from __future__ import annotations

import json
from typing import Any

from tokenpak.companion.mcp import tools


def test_vault_retrieve_path_calls_block_endpoint_directly(monkeypatch) -> None:
    calls: list[tuple[str, dict[str, Any] | None]] = []

    def fake_get(path: str, params: dict[str, Any] | None = None):
        calls.append((path, params))
        return 200, {
            "block_id": "block-a",
            "path": "docs/a.md",
            "source_path": "docs/a.md",
            "tokens": 17,
            "content": "alpha",
            "resolution": "exact_path",
        }

    monkeypatch.setattr(tools, "_proxy_get", fake_get)

    body = json.loads(tools._handle_vault_retrieve(None, {"path": "docs/a.md"}))

    assert calls == [("/tpk/v1/vault/block/", {"path": "docs/a.md"})]
    assert body["block_id"] == "block-a"
    assert body["path"] == "docs/a.md"
    assert body["tokens"] == 17
    assert body["resolution"] == "exact_path"


def test_vault_retrieve_block_id_keeps_existing_route(monkeypatch) -> None:
    calls: list[tuple[str, dict[str, Any] | None]] = []

    def fake_get(path: str, params: dict[str, Any] | None = None):
        calls.append((path, params))
        return 200, {
            "block_id": "folder/block",
            "path": "docs/a.md",
            "source_path": "docs/a.md",
            "tokens": 17,
            "content": "alpha",
            "resolution": "exact_block_id",
        }

    monkeypatch.setattr(tools, "_proxy_get", fake_get)

    body = json.loads(tools._handle_vault_retrieve(None, {"block_id": "folder/block"}))

    assert calls == [("/tpk/v1/vault/block/folder%2Fblock", None)]
    assert body["path"] == "docs/a.md"
    assert body["tokens"] == 17
    assert body["resolution"] == "exact_block_id"
