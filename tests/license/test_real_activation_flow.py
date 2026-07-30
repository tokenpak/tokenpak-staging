# SPDX-License-Identifier: Apache-2.0
"""End-to-end activation flow tests for the current license surface."""

from __future__ import annotations

import json
import os
import stat
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from tokenpak import licensing
from tokenpak.licensing import (
    TIER_FREE,
    TIER_PRO,
    activate,
    daemon_probe,
    deactivate,
    is_feature_enabled,
    load_license,
)

_PRO_FEATURE = next(feature for feature, tier in licensing._GATES.items() if tier == TIER_PRO)


@pytest.fixture(autouse=True)
def _sandbox_license(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENPAK_LICENSE_FILE", str(tmp_path / "license.json"))
    monkeypatch.delenv("TOKENPAK_LICENSE_DEV_SHIM", raising=False)
    monkeypatch.setattr(
        daemon_probe,
        "detect_daemon_state",
        lambda **_: "unavailable",
    )


def _start_feature_server(payload: dict) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path != "/v1/features":
                self.send_response(404)
                self.end_headers()
                return
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):  # noqa: A002
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    server._tokenpak_thread = thread
    return server


def _stop_feature_server(server: ThreadingHTTPServer) -> None:
    server.shutdown()
    server.server_close()
    server._tokenpak_thread.join(timeout=5)


def _route_daemon_to_server(monkeypatch, tmp_path, server: ThreadingHTTPServer):
    info_path = tmp_path / "daemon-sock-info.json"
    info_path.write_text(
        json.dumps({"port": server.server_address[1]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        daemon_probe,
        "detect_daemon_state",
        lambda **_: "active",
    )
    monkeypatch.setattr(daemon_probe, "sock_info_path", lambda: info_path)


def test_public_activation_stores_pending_license_without_unlocking_paid_feature():
    key = "PLAUSIBLE-LOOKING-KEY-1234567890"

    result = activate(key, email="buyer@example.com")

    assert result.ok is True
    assert result.license is not None
    assert result.license.tier == TIER_FREE
    assert result.license.status == "pending_validation"
    assert is_feature_enabled(_PRO_FEATURE) is False

    stored = load_license()
    assert stored.key == key
    assert stored.email == "buyer@example.com"
    assert stored.tier == TIER_FREE
    assert stored.status == "pending_validation"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions only")
def test_activation_writes_owner_only_license_file(tmp_path):
    activate("PLAUSIBLE-LOOKING-KEY-1234567890")

    mode = stat.S_IMODE((tmp_path / "license.json").stat().st_mode)

    assert mode == 0o600


def test_daemon_verified_activation_unlocks_paid_feature(monkeypatch, tmp_path):
    payload = {
        "signature": {"verified": True, "key_is_placeholder": False},
        "is_valid": True,
        "tier": TIER_PRO,
    }
    server = _start_feature_server(payload)
    _route_daemon_to_server(monkeypatch, tmp_path, server)

    try:
        result = activate("PLAUSIBLE-LOOKING-KEY-1234567890")
    finally:
        _stop_feature_server(server)

    assert result.ok is True
    assert result.license is not None
    assert result.license.tier == TIER_PRO
    assert result.license.status == "active"
    assert is_feature_enabled(_PRO_FEATURE) is True


def test_daemon_unverified_activation_stays_free(monkeypatch, tmp_path):
    payload = {
        "signature": {
            "verified": False,
            "key_is_placeholder": False,
            "reason": "unknown_key",
        },
        "is_valid": False,
        "tier": TIER_PRO,
    }
    server = _start_feature_server(payload)
    _route_daemon_to_server(monkeypatch, tmp_path, server)

    try:
        result = activate("PLAUSIBLE-LOOKING-KEY-1234567890")
    finally:
        _stop_feature_server(server)

    assert result.ok is True
    assert result.license is not None
    assert result.license.tier == TIER_FREE
    assert result.license.status == "pending_validation"
    assert is_feature_enabled(_PRO_FEATURE) is False


def test_deactivate_reverts_activation_to_free():
    activate("PLAUSIBLE-LOOKING-KEY-1234567890")

    assert deactivate() is True
    assert load_license().tier == TIER_FREE
    assert is_feature_enabled(_PRO_FEATURE) is False
