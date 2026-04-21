"""TokenPak byte-fidelity benchmark harness.

Runs canonical scenarios against a locally-started `tokenpak serve`. Captures
byte streams for baseline creation and diffs against them for stage-migration
verification.

Usage:
    python3 examples/benchmarks/harness.py --capture-baseline
    python3 examples/benchmarks/harness.py --verify

Authorization model (OAuth, no API keys):
  - Claude Code CLI stores an OAuth access token in ~/.claude/.credentials.json
    under claudeAiOauth.accessToken (format: sk-ant-oat01-...).
  - OpenAI Codex CLI stores an OAuth token in ~/.codex/auth.json.
  - The harness reads these directly and injects them as Bearer tokens
    into outbound requests, matching the exact shape Claude Code / Codex
    CLI send in production.
  - The proxy's oauth.py forwards the Bearer header transparently to the
    upstream provider. Same shape, same byte-level flow.

Scenarios live in scenarios/NN-name/ directories, each containing:
    request.json   — path + method + headers + body (no auth — harness injects)
    metadata.json  — provider, OAuth source, profile, expected headers

Baselines land under baselines/NN-name/:
    request.bin    — outbound JSON body bytes (what the proxy received)
    response.bin   — inbound response body bytes
    headers.json   — {X-TokenPak-*} response headers + status
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HARNESS_DIR = Path(__file__).resolve().parent
SCENARIOS_DIR = HARNESS_DIR / "scenarios"
BASELINES_DIR = HARNESS_DIR / "baselines"

# Use a harness-specific port to avoid collision with any production
# `tokenpak serve` that might be running for real Claude Code traffic on
# the default 8766.
PROXY_HOST = os.environ.get("TOKENPAK_BENCHMARK_HOST", "127.0.0.1")
PROXY_PORT = int(os.environ.get("TOKENPAK_BENCHMARK_PORT", "8867"))
PROXY_URL = f"http://{PROXY_HOST}:{PROXY_PORT}"

PROXY_START_TIMEOUT_S = 20


def _load_scenario(scenario_dir: Path) -> dict:
    req_path = scenario_dir / "request.json"
    meta_path = scenario_dir / "metadata.json"
    if not req_path.exists():
        raise FileNotFoundError(f"scenario missing request.json: {scenario_dir}")
    with open(req_path) as f:
        req = json.load(f)
    meta = {}
    if meta_path.exists():
        with open(meta_path) as f:
            meta = json.load(f)
    return {"id": scenario_dir.name, "request": req, "metadata": meta}


def _discover_scenarios() -> list[dict]:
    if not SCENARIOS_DIR.exists():
        return []
    out = []
    for child in sorted(SCENARIOS_DIR.iterdir()):
        if child.is_dir() and (child / "request.json").exists():
            out.append(_load_scenario(child))
    return out


def _start_proxy() -> subprocess.Popen:
    """Start `tokenpak serve` from the dev tree on the harness port."""
    env = os.environ.copy()
    env.setdefault("TOKENPAK_PORT", str(PROXY_PORT))
    # Run via `python3 -m tokenpak.proxy` against the dev tree so
    # we benchmark the current checkout, not whatever pip-installed version
    # happens to be on PATH.
    # Invoke via `python -c "from tokenpak.proxy.server import start_proxy; start_proxy(...)"`
    # which works against the dev tree (the canonical tokenpak.proxy package, not the
    # legacy tokenpak.proxy module that `python -m tokenpak.proxy` resolves to in
    # installed 1.0.3 packages).
    bootstrap = (
        f"from tokenpak.proxy.server import start_proxy; "
        f"start_proxy(host={PROXY_HOST!r}, port={PROXY_PORT!r})"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", bootstrap],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    # Wait for readiness by hitting any /v1/* path — proxy returns 401 without
    # auth, which is a "listening" signal. A true healthz route would be cleaner
    # but this works across the current proxy's route set.
    deadline = time.time() + PROXY_START_TIMEOUT_S
    while time.time() < deadline:
        if proc.poll() is not None:
            # Process exited — read output for diagnostics
            stdout, stderr = proc.communicate(timeout=5)
            raise RuntimeError(
                f"tokenpak proxy exited with code {proc.returncode}.\n"
                f"stdout: {stdout.decode()[:2000]}\nstderr: {stderr.decode()[:2000]}"
            )
        try:
            req = urllib.request.Request(f"{PROXY_URL}/v1/messages", method="GET")
            with urllib.request.urlopen(req, timeout=1) as resp:
                return proc  # 200 — proxy listening
        except urllib.error.HTTPError:
            # 401/404/405 all signal "listening but this route/method rejected" — good
            return proc
        except Exception:
            time.sleep(0.3)
    proc.terminate()
    stdout, stderr = proc.communicate(timeout=5)
    raise RuntimeError(
        f"tokenpak proxy did not become healthy in {PROXY_START_TIMEOUT_S}s.\n"
        f"stdout: {stdout.decode()[:2000]}\nstderr: {stderr.decode()[:2000]}"
    )


def _stop_proxy(proc: subprocess.Popen) -> None:
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def _claude_oauth_token() -> str | None:
    """Read Claude Code OAuth access token from ~/.claude/.credentials.json."""
    path = Path.home() / ".claude" / ".credentials.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        return data.get("claudeAiOauth", {}).get("accessToken")
    except Exception:
        return None


def _codex_oauth_token() -> str | None:
    """Read OpenAI Codex OAuth access token from ~/.codex/auth.json."""
    path = Path.home() / ".codex" / "auth.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        # Codex CLI shape varies; try common locations
        for key in ("access_token", "accessToken"):
            if key in data:
                return data[key]
        tokens = data.get("tokens", {})
        if isinstance(tokens, dict):
            for key in ("access_token", "accessToken"):
                if key in tokens:
                    return tokens[key]
    except Exception:
        pass
    return None


def _inject_auth(headers: dict, oauth_source: str) -> dict:
    """Inject OAuth Bearer token matching the provider's production format."""
    headers = dict(headers)  # copy
    if oauth_source == "claude-code":
        token = _claude_oauth_token()
        if not token:
            raise RuntimeError(
                "Claude Code OAuth not found at ~/.claude/.credentials.json. "
                "Run `claude auth login` first."
            )
        # Claude Code sends Bearer in Authorization per the Anthropic API
        headers["Authorization"] = f"Bearer {token}"
        # Claude Code sends anthropic-beta=oauth-2025-04-20 for OAuth Subscriber
        # usage. Match that header shape precisely.
        headers["anthropic-beta"] = "oauth-2025-04-20"
        # Match Claude Code CLI's User-Agent — Anthropic rate-limits generic
        # Python urllib agents as suspected abuse.
        headers.setdefault("User-Agent", "claude-cli/2.0.0 (external, cli)")
        # Claude Code also strips x-api-key when using OAuth; ensure it's not set.
        headers.pop("x-api-key", None)
    elif oauth_source == "codex":
        token = _codex_oauth_token()
        if not token:
            raise RuntimeError(
                "Codex OAuth not found at ~/.codex/auth.json. "
                "Run `codex auth login` first."
            )
        headers["Authorization"] = f"Bearer {token}"
    else:
        raise ValueError(f"unknown oauth_source: {oauth_source!r}")
    return headers


def _execute_scenario(scenario: dict) -> dict:
    """Run one scenario through the local proxy; capture bytes + headers."""
    req = scenario["request"]
    meta = scenario.get("metadata", {})
    oauth_source = meta.get("oauth_source", "claude-code")
    # Build request to the proxy (proxy forwards to upstream)
    url = f"{PROXY_URL}{req['path']}"
    method = req.get("method", "POST").upper()
    headers = _inject_auth(req.get("headers", {}), oauth_source)
    body = req.get("body")

    body_bytes: bytes | None
    if body is None:
        body_bytes = None
    elif isinstance(body, str):
        body_bytes = body.encode("utf-8")
    else:
        body_bytes = json.dumps(body).encode("utf-8")

    py_req = urllib.request.Request(url, method=method, data=body_bytes, headers=headers)
    try:
        with urllib.request.urlopen(py_req, timeout=60) as resp:
            resp_body = resp.read()
            resp_headers = {k: v for k, v in resp.headers.items()}
            status = resp.status
    except urllib.error.HTTPError as e:
        resp_body = e.read() if hasattr(e, "read") else b""
        resp_headers = dict(e.headers or {})
        status = e.code
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}", "status": -1}

    tpk_headers = {k: v for k, v in resp_headers.items() if k.lower().startswith("x-tokenpak-")}

    return {
        "status": status,
        "request_bytes": body_bytes or b"",
        "response_bytes": resp_body,
        "tokenpak_headers": tpk_headers,
        "all_headers": resp_headers,
    }


def _capture_one(scenario: dict, out_dir: Path) -> None:
    result = _execute_scenario(scenario)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "request.bin").write_bytes(result.get("request_bytes", b""))
    (out_dir / "response.bin").write_bytes(result.get("response_bytes", b""))
    (out_dir / "headers.json").write_text(
        json.dumps(
            {
                "status": result.get("status"),
                "tokenpak_headers": result.get("tokenpak_headers", {}),
                "all_headers_sha256": hashlib.sha256(
                    json.dumps(result.get("all_headers", {}), sort_keys=True).encode()
                ).hexdigest()[:16],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _diff_one(scenario: dict) -> dict:
    baseline_dir = BASELINES_DIR / scenario["id"]
    if not baseline_dir.exists():
        return {"status": "MISSING-BASELINE", "diffs": []}
    result = _execute_scenario(scenario)
    diffs: list[str] = []

    base_req = (baseline_dir / "request.bin").read_bytes()
    if base_req != result.get("request_bytes", b""):
        diffs.append(
            f"request.bin drift: baseline={_sha256_bytes(base_req)[:16]} "
            f"post={_sha256_bytes(result.get('request_bytes', b''))[:16]}"
        )

    base_resp = (baseline_dir / "response.bin").read_bytes()
    if base_resp != result.get("response_bytes", b""):
        diffs.append(
            f"response.bin drift: baseline={_sha256_bytes(base_resp)[:16]} "
            f"post={_sha256_bytes(result.get('response_bytes', b''))[:16]}"
        )

    base_hdr = json.loads((baseline_dir / "headers.json").read_text())
    post_tpk = result.get("tokenpak_headers", {})
    for k, v in base_hdr.get("tokenpak_headers", {}).items():
        if post_tpk.get(k) != v:
            diffs.append(f"{k}: baseline={v!r} post={post_tpk.get(k)!r}")

    return {"status": "PASS" if not diffs else "DRIFT", "diffs": diffs}


def cmd_capture_baseline() -> int:
    scenarios = _discover_scenarios()
    if not scenarios:
        print("no scenarios found; add at least one under examples/benchmarks/scenarios/", file=sys.stderr)
        return 2
    print(f"[harness] starting tokenpak serve on {PROXY_URL} ...")
    proc = _start_proxy()
    try:
        for scenario in scenarios:
            print(f"[harness] capturing {scenario['id']} ...")
            try:
                _capture_one(scenario, BASELINES_DIR / scenario["id"])
                print(f"  OK -> baselines/{scenario['id']}/")
            except Exception as e:
                print(f"  ERROR: {type(e).__name__}: {e}")
    finally:
        _stop_proxy(proc)
    print("[harness] baseline capture complete.")
    return 0


def cmd_verify() -> int:
    scenarios = _discover_scenarios()
    if not scenarios:
        print("no scenarios found", file=sys.stderr)
        return 2
    print(f"[harness] starting tokenpak serve on {PROXY_URL} ...")
    proc = _start_proxy()
    any_drift = False
    try:
        for scenario in scenarios:
            print(f"[harness] verifying {scenario['id']} ...")
            verdict = _diff_one(scenario)
            if verdict["status"] == "PASS":
                print(f"  PASS")
            elif verdict["status"] == "MISSING-BASELINE":
                print(f"  MISSING-BASELINE (run --capture-baseline first)")
                any_drift = True
            else:
                print(f"  DRIFT:")
                for d in verdict["diffs"]:
                    print(f"    {d}")
                any_drift = True
    finally:
        _stop_proxy(proc)
    print("[harness] verify complete — " + ("FAIL (drifts found)" if any_drift else "PASS (no drifts)"))
    return 1 if any_drift else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--capture-baseline", action="store_true")
    mode.add_argument("--verify", action="store_true")
    args = ap.parse_args()

    if args.capture_baseline:
        return cmd_capture_baseline()
    if args.verify:
        return cmd_verify()
    return 2


if __name__ == "__main__":
    sys.exit(main())
