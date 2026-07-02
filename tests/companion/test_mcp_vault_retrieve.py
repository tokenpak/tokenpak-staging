# SPDX-License-Identifier: Apache-2.0
"""MCP vault_retrieve adapter coverage."""

from __future__ import annotations

import json

from tokenpak.companion.mcp import tools


def test_vault_retrieve_block_id_quotes_slashes(monkeypatch) -> None:
    calls: list[tuple[str, dict[str, str] | None]] = []

    def fake_get(path: str, params: dict[str, str] | None = None):
        calls.append((path, params))
        return 200, {
            "block_id": "folder/block",
            "path": "docs/a.md",
            "tokens": 17,
            "content": "alpha",
            "resolution": "exact_block_id",
        }

    monkeypatch.setattr(tools, "_proxy_get", fake_get)

    body = json.loads(tools._handle_vault_retrieve(None, {"block_id": "folder/block"}))

    assert calls == [("/tpk/v1/vault/block/folder%2Fblock", None)]
    assert body["block_id"] == "folder/block"
    assert body["path"] == "docs/a.md"
    assert body["tokens"] == 17
    assert body["resolution"] == "exact_block_id"
