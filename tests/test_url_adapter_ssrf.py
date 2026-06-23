# SPDX-License-Identifier: Apache-2.0
"""SSRF regression tests for connector and content auto-fetch paths.

Covers:
  * byte/size cap on response bodies (url_adapter + reused by _cli_core)
  * content-embedded private/metadata URL is never auto-fetched (compiler)
  * _cli_core inline config fetch reuses the URL-adapter SSRF guard
  * redirect targets are re-validated (302 -> metadata blocked)

These extend the existing URL safety guard instead of defining another one.
"""

import types
from unittest.mock import MagicMock

import pytest

from tokenpak.compression.reference_scanner import Reference, RefType
from tokenpak.sources import url_adapter
from tokenpak.sources.url_adapter import (
    _MAX_RESPONSE_BYTES,
    SourceFetchError,
    URLAdapter,
    _read_capped,
    _SafeRedirectHandler,
    _validate_url_safe,
)

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _FakeHeaders:
    def __init__(self, mapping):
        self._m = mapping

    def get(self, key, default=""):
        return self._m.get(key, default)


class _FakeResp:
    """Minimal urllib-response stand-in honouring read(n) bounded reads."""

    def __init__(self, body: bytes, headers=None):
        self._buf = body
        self._pos = 0
        self.headers = _FakeHeaders(headers or {})

    def read(self, n=-1):
        if n is None or n < 0:
            chunk = self._buf[self._pos:]
            self._pos = len(self._buf)
            return chunk
        chunk = self._buf[self._pos:self._pos + n]
        self._pos += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# ---------------------------------------------------------------------------
# Existing URL guard — exercised through each fetch path
# ---------------------------------------------------------------------------


def test_validate_blocks_file_scheme():
    with pytest.raises(SourceFetchError):
        _validate_url_safe("file:///etc/passwd")


def test_validate_blocks_loopback():
    with pytest.raises(SourceFetchError):
        _validate_url_safe("http://127.0.0.1/admin")


def test_validate_blocks_metadata_ip():
    with pytest.raises(SourceFetchError):
        _validate_url_safe("http://169.254.169.254/latest/meta-data/")


def test_validate_blocks_localhost():
    with pytest.raises(SourceFetchError):
        _validate_url_safe("http://localhost:8080/")


def test_validate_allows_public(monkeypatch):
    # Avoid real DNS — resolve example.com to a public address.
    monkeypatch.setattr(
        url_adapter.socket,
        "getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))],
    )
    _validate_url_safe("https://example.com/page")  # must not raise


# ---------------------------------------------------------------------------
# Rider #4 — redirect targets are re-validated before being followed
# ---------------------------------------------------------------------------


def test_redirect_handler_blocks_metadata_target():
    handler = _SafeRedirectHandler()
    with pytest.raises(SourceFetchError):
        handler.redirect_request(
            None, None, 302, "Found", {}, "http://169.254.169.254/latest/meta-data/"
        )


# ---------------------------------------------------------------------------
# Rider #1 — byte/size cap
# ---------------------------------------------------------------------------


def test_read_capped_truncates_oversized():
    resp = _FakeResp(b"a" * 250)
    out = _read_capped(resp, max_bytes=100)
    assert len(out) == 100


def test_read_capped_returns_full_when_under_cap():
    resp = _FakeResp(b"a" * 40)
    out = _read_capped(resp, max_bytes=100)
    assert out == b"a" * 40


def test_ingest_applies_byte_cap(monkeypatch):
    real = url_adapter._read_capped
    seen = {}

    def _spy(resp, max_bytes=_MAX_RESPONSE_BYTES):
        seen["called"] = True
        return real(resp, max_bytes=64)  # force a tiny cap for the assertion

    monkeypatch.setattr(url_adapter, "_check_robots", lambda url: True)
    monkeypatch.setattr(url_adapter, "_validate_url_safe", lambda url: None)
    monkeypatch.setattr(
        url_adapter,
        "_urlopen_checked",
        lambda req, timeout: _FakeResp(b"y" * 5000, {"Content-Type": "text/plain"}),
    )
    monkeypatch.setattr(url_adapter, "_read_capped", _spy)

    content, _prov = URLAdapter().ingest("http://example.com/")
    assert seen.get("called") is True
    assert len(content) == 64


# ---------------------------------------------------------------------------
# Rider #2 — content-embedded metadata URL is never auto-fetched (compiler)
# ---------------------------------------------------------------------------


def _meta_ref():
    return Reference(
        ref_type=RefType.URL,
        raw_match="http://169.254.169.254/",
        resolved_url="http://169.254.169.254/latest/meta-data/",
    )


def test_compiler_is_safe_ref_blocks_metadata():
    from tokenpak.compression.compiler import _is_safe_ref

    assert _is_safe_ref(_meta_ref()) is False


def test_compiler_is_safe_ref_allows_public(monkeypatch):
    from tokenpak.compression.compiler import _is_safe_ref

    monkeypatch.setattr(
        url_adapter.socket,
        "getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))],
    )
    ref = Reference(
        ref_type=RefType.URL,
        raw_match="https://example.com/",
        resolved_url="https://example.com/doc",
    )
    assert _is_safe_ref(ref) is True


def test_compiler_does_not_autofetch_metadata_url(monkeypatch):
    from tokenpak.compression import compiler

    fetched = []
    monkeypatch.setattr(
        compiler, "fetch_reference", lambda ref: fetched.append(ref) or "DATA"
    )
    monkeypatch.setattr(compiler, "scan_for_references", lambda text: [_meta_ref()])

    out = compiler.compile_with_refs(
        [{"content": "irrelevant", "tokens": 1}],
        query="please read http://169.254.169.254/latest/meta-data/",
        budget=1000,
    )
    assert fetched == []  # the metadata URL must never be auto-fetched
    assert isinstance(out, str)


# ---------------------------------------------------------------------------
# Rider #3 — _cli_core inline config fetch reuses the SSRF guard
# ---------------------------------------------------------------------------


def test_cli_config_sync_blocks_metadata_url(monkeypatch, capsys):
    from tokenpak import _cli_core

    # If the guard works the opener must never be reached.
    opener = MagicMock()
    monkeypatch.setattr(url_adapter, "_urlopen_checked", opener)

    args = types.SimpleNamespace(
        source="url",
        url="http://169.254.169.254/latest/meta-data/",
        dry_run=True,
    )
    _cli_core.cmd_config_sync(args)

    out = capsys.readouterr().out
    assert "Failed to fetch config" in out
    opener.assert_not_called()
