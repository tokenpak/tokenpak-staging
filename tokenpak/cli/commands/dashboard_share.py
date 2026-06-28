# SPDX-License-Identifier: Apache-2.0
"""Guided dashboard sharing helpers."""

from __future__ import annotations

import re
import shutil
import socket
import subprocess
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, TextIO

__all__: list[str] = []

TRYCLOUDFLARE_RE = re.compile(r"https://[A-Za-z0-9.-]+\.trycloudflare\.com\b")


@dataclass(frozen=True)
class DashboardSharePlan:
    """A user-facing plan for sharing the local dashboard."""

    port: int
    token: str
    local_url: str
    lan_url: str | None
    proxy_running: bool
    cloudflared_path: str | None
    cloudflared_config_present: bool


def dashboard_url(base_url: str, token: str) -> str:
    """Return a dashboard URL with a token query parameter."""
    encoded_token = urllib.parse.quote(token, safe="")
    return f"{base_url.rstrip('/')}/dashboard?token={encoded_token}"


def detect_lan_url(port: int) -> str | None:
    """Best-effort same-network URL without creating outbound traffic."""
    try:
        hostname = socket.gethostname()
        ip = socket.gethostbyname(hostname)
    except Exception:
        return None

    if not ip or ip.startswith("127."):
        return None
    return f"http://{ip}:{port}"


def is_dashboard_running(port: int, timeout: float = 0.75) -> bool:
    """Return whether the local proxy/dashboard responds to a health probe."""
    try:
        url = f"http://127.0.0.1:{port}/health"
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status < 500
    except Exception:
        return False


def cloudflared_config_present(home: Path | None = None) -> bool:
    """Return whether a local cloudflared config may affect quick tunnels."""
    root = home or Path.home()
    return (root / ".cloudflared" / "config.yaml").exists()


def build_share_plan(
    *,
    port: int,
    token: str,
    cloudflared_path: str | None = None,
    lan_url: str | None = None,
    proxy_running: bool | None = None,
    home: Path | None = None,
) -> DashboardSharePlan:
    """Build the guided plan shown by ``tokenpak dashboard --public``."""
    local_base = f"http://127.0.0.1:{port}"
    detected_lan_url = lan_url if lan_url is not None else detect_lan_url(port)
    return DashboardSharePlan(
        port=port,
        token=token,
        local_url=dashboard_url(local_base, token),
        lan_url=dashboard_url(detected_lan_url, token) if detected_lan_url else None,
        proxy_running=is_dashboard_running(port) if proxy_running is None else proxy_running,
        cloudflared_path=cloudflared_path
        if cloudflared_path is not None
        else shutil.which("cloudflared"),
        cloudflared_config_present=cloudflared_config_present(home),
    )


def render_share_plan(plan: DashboardSharePlan) -> str:
    """Render a concise, copy-pasteable dashboard sharing plan."""
    lines = [
        "TokenPak Dashboard Share",
        "------------------------",
        "",
        "Local access:",
        f"  {plan.local_url}",
        "",
    ]

    if plan.proxy_running:
        lines.extend(["Status: dashboard is responding locally.", ""])
    else:
        lines.extend(
            [
                "Status: dashboard is not responding on localhost yet.",
                "Start it with: tokenpak start",
                "",
            ]
        )

    lines.extend(["Remote options:", ""])

    if plan.lan_url:
        lines.extend(
            [
                "1. Same network or VPN",
                f"   {plan.lan_url}",
                "",
            ]
        )
    else:
        lines.extend(
            [
                "1. Same network or VPN",
                "   No LAN address was detected. Local access still works.",
                "",
            ]
        )

    lines.extend(
        [
            "2. Temporary internet link, no Cloudflare account required",
            "   tokenpak dashboard --public --tunnel",
        ]
    )
    if plan.cloudflared_path:
        lines.append(f"   cloudflared detected at: {plan.cloudflared_path}")
    else:
        lines.append("   cloudflared is not installed yet.")
    if plan.cloudflared_config_present:
        lines.append("   Note: an existing ~/.cloudflared/config.yaml may affect quick tunnels.")
    lines.append("")

    lines.extend(
        [
            "3. Managed Cloudflare tunnel",
            f"   Point your tunnel origin at: http://127.0.0.1:{plan.port}",
            "   Then share: https://<your-host>/dashboard?token=<token>",
            "",
            "Security:",
            "  Share links only with trusted viewers.",
            "  Rotate the token with: tokenpak dashboard --new-token",
        ]
    )

    return "\n".join(lines)


def extract_trycloudflare_url(text: str) -> str | None:
    """Extract the first TryCloudflare URL from cloudflared output."""
    match = TRYCLOUDFLARE_RE.search(text)
    return match.group(0) if match else None


def run_quick_tunnel(
    *,
    port: int,
    token: str,
    cloudflared_path: str | None = None,
    popen_factory: Callable[..., subprocess.Popen] = subprocess.Popen,
    stream: TextIO | None = None,
) -> int:
    """Start a temporary Cloudflare quick tunnel and print the share URL."""
    output = stream or sys.stdout
    executable = cloudflared_path or shutil.which("cloudflared")
    if not executable:
        output.write(
            "cloudflared is not installed.\n"
            "Local dashboard access still works with `tokenpak dashboard --public`.\n"
        )
        return 1

    origin = f"http://127.0.0.1:{port}"
    output.write("Starting a temporary Cloudflare quick tunnel...\n")
    output.write(f"Origin: {origin}\n")
    output.write("Press Ctrl-C to stop sharing.\n\n")
    proc = popen_factory(
        [executable, "tunnel", "--url", origin],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    seen_share_url: str | None = None
    stdout: Iterable[str] = proc.stdout or []
    for line in stdout:
        output.write(line)
        maybe_url = extract_trycloudflare_url(line)
        if maybe_url and maybe_url != seen_share_url:
            seen_share_url = maybe_url
            output.write(f"\nShare URL: {dashboard_url(maybe_url, token)}\n\n")

    return proc.wait()
