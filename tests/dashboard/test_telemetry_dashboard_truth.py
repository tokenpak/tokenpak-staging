"""Telemetry dashboard privacy/truth regression tests."""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TELEMETRY_DASHBOARD = ROOT / "tokenpak" / "telemetry" / "dashboard"
DESIGN_TOKENS = TELEMETRY_DASHBOARD / "static" / "css" / "design-tokens.css"
SETTINGS_UI = TELEMETRY_DASHBOARD / "static" / "js" / "settings-ui.js"


def test_design_tokens_do_not_import_remote_fonts() -> None:
    css = DESIGN_TOKENS.read_text(encoding="utf-8")

    assert "fonts.googleapis.com" not in css
    assert "gstatic.com" not in css
    assert "@import url('http" not in css
    assert '@import url("http' not in css


def test_data_settings_do_not_expose_fake_runtime_controls() -> None:
    js = SETTINGS_UI.read_text(encoding="utf-8")

    assert "Segment Capture Mode" not in js
    assert "Debug Sampling Rate" not in js
    assert "Retention Period" not in js
    assert "captureMode" not in js
    assert "debugSamplingRate" not in js
    assert "retentionPeriod" not in js
    assert "settings/system/db-size" not in js
    assert "settings/system/clear-data" not in js
    assert "Runtime capture, sampling, retention, and deletion are managed outside the dashboard." in js


def test_settings_save_endpoint_reports_unsupported() -> None:
    pytest.importorskip("fastapi", reason="fastapi is optional")
    pytest.importorskip("starlette", reason="starlette is optional")

    from fastapi import FastAPI
    from starlette.testclient import TestClient

    from tokenpak.telemetry.dashboard.dashboard import create_dashboard_router

    app = FastAPI()
    app.include_router(create_dashboard_router(storage=object(), rollups=object()))

    with TestClient(app, raise_server_exceptions=True) as client:
        response = client.post(
            "/dashboard/settings/save",
            json={"data": {"captureMode": "payload", "retentionPeriod": "90d"}},
        )

    assert response.status_code == 501
    body = response.json()
    assert body["status"] == "unsupported"
    assert "display preferences only" in body["message"]
