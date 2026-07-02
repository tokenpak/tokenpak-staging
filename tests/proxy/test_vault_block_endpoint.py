# SPDX-License-Identifier: Apache-2.0
"""Offline regression coverage for the vault block REST endpoint."""

from __future__ import annotations

import io
import json
from types import SimpleNamespace
from typing import Any

from tokenpak.proxy import app_endpoints


class _StubHandler:
    def __init__(self, path: str):
        self.path = path
        self.client_address = ("127.0.0.1", 0)
        self.headers: dict[str, str] = {}
        self.wfile = io.BytesIO()
        self._status: int | None = None

    def send_response(self, code: int) -> None:
        self._status = code

    def send_header(self, name: str, value: str) -> None:
        pass

    def end_headers(self) -> None:
        pass

    def response_status(self) -> int:
        assert self._status is not None
        return self._status

    def response_json(self) -> dict[str, Any]:
        return json.loads(self.wfile.getvalue().decode("utf-8"))


def _call(path: str) -> _StubHandler:
    handler = _StubHandler(path)
    assert app_endpoints.try_handle_get(handler) is True
    return handler


def test_encoded_block_id_returns_vault_block(tmp_path, monkeypatch) -> None:
    block_id = "folder/block"
    blocks_dir = tmp_path / "blocks" / "folder"
    blocks_dir.mkdir(parents=True)
    (blocks_dir / "block.txt").write_text("alpha", encoding="utf-8")
    vault_index = SimpleNamespace(
        available=True,
        tokenpak_dir=tmp_path,
        blocks={
            block_id: {
                "path": "docs/folder/block.md",
                "tokens": 17,
            }
        },
    )
    monkeypatch.setattr(
        "tokenpak.proxy.vault_bridge.get_vault_index",
        lambda: vault_index,
    )

    response = _call("/tpk/v1/vault/block/folder%2Fblock")

    assert response.response_status() == 200
    body = response.response_json()
    assert body["block_id"] == block_id
    assert body["path"] == "docs/folder/block.md"
    assert body["tokens"] == 17
    assert body["content"] == "alpha"
