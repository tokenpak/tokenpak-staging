# SPDX-License-Identifier: Apache-2.0
"""Tests for guided dashboard sharing helpers."""

from __future__ import annotations

from io import StringIO

from tokenpak.cli.commands.dashboard_share import (
    DashboardSharePlan,
    build_share_plan,
    dashboard_url,
    extract_trycloudflare_url,
    render_share_plan,
    run_quick_tunnel,
)


def test_dashboard_url_encodes_token() -> None:
    assert (
        dashboard_url("https://example.test", "tok en/plus+")
        == "https://example.test/dashboard?token=tok%20en%2Fplus%2B"
    )


def test_build_share_plan_accepts_no_cloudflare_account_path(tmp_path) -> None:
    cloudflared_dir = tmp_path / ".cloudflared"
    cloudflared_dir.mkdir()
    (cloudflared_dir / "config.yaml").write_text("tunnel: example\n")

    plan = build_share_plan(
        port=8766,
        token="abc",
        cloudflared_path="/usr/local/bin/cloudflared",
        lan_url="http://192.0.2.10:8766",
        proxy_running=True,
        home=tmp_path,
    )

    assert plan.local_url == "http://127.0.0.1:8766/dashboard?token=abc"
    assert plan.lan_url == "http://192.0.2.10:8766/dashboard?token=abc"
    assert plan.proxy_running is True
    assert plan.cloudflared_path == "/usr/local/bin/cloudflared"
    assert plan.cloudflared_config_present is True


def test_render_share_plan_does_not_claim_machine_global_access() -> None:
    plan = DashboardSharePlan(
        port=8766,
        token="abc",
        local_url="http://127.0.0.1:8766/dashboard?token=abc",
        lan_url=None,
        proxy_running=False,
        cloudflared_path=None,
        cloudflared_config_present=False,
    )

    text = render_share_plan(plan)

    assert "Local access:" in text
    assert "Start it with: tokenpak start" in text
    assert "Temporary internet link, no Cloudflare account required" in text
    assert "cloudflared is not installed yet" in text
    assert "accessible from any machine" not in text


def test_extract_trycloudflare_url() -> None:
    assert (
        extract_trycloudflare_url("Visit https://alpha-beta.trycloudflare.com for access")
        == "https://alpha-beta.trycloudflare.com"
    )
    assert extract_trycloudflare_url("no public URL yet") is None


def test_run_quick_tunnel_prints_dashboard_share_url() -> None:
    calls = []

    class FakeProc:
        stdout = iter(["info: https://demo.trycloudflare.com\n"])

        @staticmethod
        def wait() -> int:
            return 0

    def fake_popen(args, **kwargs):
        calls.append((args, kwargs))
        return FakeProc()

    stream = StringIO()
    rc = run_quick_tunnel(
        port=8766,
        token="abc",
        cloudflared_path="/bin/cloudflared",
        popen_factory=fake_popen,
        stream=stream,
    )

    assert rc == 0
    assert calls[0][0] == [
        "/bin/cloudflared",
        "tunnel",
        "--url",
        "http://127.0.0.1:8766",
    ]
    assert "Share URL: https://demo.trycloudflare.com/dashboard?token=abc" in stream.getvalue()
