#!/usr/bin/env python3
"""Integration detector — safe local-signal inspection.

Implements proposal §S2.2 / §S2.3. The detector inspects SAFE local signals
only — no network, no provider calls, no reading of secret values from config
files. Just presence / non-presence of well-known env vars and config paths.

Detected personas:

  * Anthropic SDK user           — `ANTHROPIC_API_KEY` or `ANTHROPIC_BASE_URL`
  * OpenAI / Codex-compatible    — `OPENAI_API_KEY` or `OPENAI_BASE_URL`
  * Claude Code / OpenClaw       — `~/.claude/settings.json` or `CLAUDE_CODE_OAUTH_TOKEN`
  * Cursor                       — `~/.cursor/` config dir
  * Aider                        — `aider` binary on PATH

Each detected client gets a confidence label (`high|medium|low`), a list of
`missing_steps`, and exact `next_commands` for the user to run.

Usage:
    python3 scripts/integration_detector.py            # plain
    python3 scripts/integration_detector.py --json     # machine-readable

By design, this tool is NON-INVASIVE: it never opens config files for read,
never queries running processes, never hits the network. It only checks
whether a path or env var EXISTS.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class Detection:
    client: str
    confidence: str
    signals: list[str] = field(default_factory=list)
    missing_steps: list[str] = field(default_factory=list)
    next_commands: list[str] = field(default_factory=list)


def env_present(name: str) -> bool:
    return bool(os.environ.get(name, "").strip())


def home_path(rel: str, home: Optional[Path] = None) -> Path:
    return (home or Path.home()) / rel


def detect_anthropic(home: Optional[Path] = None) -> Optional[Detection]:
    signals = []
    if env_present("ANTHROPIC_API_KEY"):
        signals.append("env:ANTHROPIC_API_KEY")
    if env_present("ANTHROPIC_BASE_URL"):
        signals.append("env:ANTHROPIC_BASE_URL")
    if not signals:
        return None
    confidence = "high" if "env:ANTHROPIC_API_KEY" in signals else "medium"
    base_url = os.environ.get("ANTHROPIC_BASE_URL", "")
    missing = []
    next_cmds = []
    if "127.0.0.1" in base_url or "localhost" in base_url:
        # Already pointed at a local proxy.
        next_cmds.append("tokenpak status   # confirm proxy is healthy")
    else:
        missing.append("ANTHROPIC_BASE_URL not pointing at the local proxy")
        next_cmds.append("tokenpak serve    # start the local proxy")
        next_cmds.append(
            "export ANTHROPIC_BASE_URL=http://127.0.0.1:8766    # route SDK through tokenpak"
        )
    return Detection(
        client="anthropic-sdk",
        confidence=confidence,
        signals=signals,
        missing_steps=missing,
        next_commands=next_cmds,
    )


def detect_openai_codex(home: Optional[Path] = None) -> Optional[Detection]:
    signals = []
    if env_present("OPENAI_API_KEY"):
        signals.append("env:OPENAI_API_KEY")
    if env_present("OPENAI_BASE_URL"):
        signals.append("env:OPENAI_BASE_URL")
    if env_present("CODEX_OAUTH_TOKEN") or env_present("OPENAI_CODEX_OAUTH"):
        signals.append("env:codex-oauth")
    if not signals:
        return None
    confidence = "high" if "env:OPENAI_API_KEY" in signals else "medium"
    base_url = os.environ.get("OPENAI_BASE_URL", "")
    missing = []
    next_cmds = []
    if "127.0.0.1" in base_url or "localhost" in base_url:
        next_cmds.append("tokenpak status   # confirm proxy is healthy")
    else:
        missing.append("OPENAI_BASE_URL not pointing at the local proxy")
        next_cmds.append("tokenpak serve    # start the local proxy")
        next_cmds.append(
            "export OPENAI_BASE_URL=http://127.0.0.1:8766/v1    # route SDK through tokenpak"
        )
    return Detection(
        client="openai-sdk-or-codex",
        confidence=confidence,
        signals=signals,
        missing_steps=missing,
        next_commands=next_cmds,
    )


def detect_claude_code(home: Optional[Path] = None) -> Optional[Detection]:
    signals = []
    settings = home_path(".claude/settings.json", home)
    if settings.exists():
        signals.append(f"path:{settings}")
    if env_present("CLAUDE_CODE_OAUTH_TOKEN"):
        signals.append("env:CLAUDE_CODE_OAUTH_TOKEN")
    if not signals:
        return None
    confidence = "high" if str(settings) in signals[0] else "medium"
    missing = []
    next_cmds = []
    next_cmds.append("tokenpak integrate claude-code --apply   # wire Claude Code to the proxy")
    next_cmds.append("tokenpak status                          # confirm the proxy is running")
    return Detection(
        client="claude-code",
        confidence=confidence,
        signals=signals,
        missing_steps=missing,
        next_commands=next_cmds,
    )


def detect_cursor(home: Optional[Path] = None) -> Optional[Detection]:
    cursor_dir = home_path(".cursor", home)
    if not cursor_dir.is_dir():
        return None
    return Detection(
        client="cursor",
        confidence="medium",
        signals=[f"path:{cursor_dir}"],
        missing_steps=[],
        next_commands=["tokenpak integrate cursor --apply"],
    )


def detect_aider(home: Optional[Path] = None) -> Optional[Detection]:
    aider = shutil.which("aider")
    if not aider:
        return None
    return Detection(
        client="aider",
        confidence="medium",
        signals=[f"path:{aider}"],
        missing_steps=[],
        next_commands=["tokenpak integrate aider --apply"],
    )


def detect_proxy_config(home: Optional[Path] = None) -> Optional[Detection]:
    cfg = home_path(".tokenpak/config.json", home)
    legacy = home_path(".config/tokenpak/config.json", home)
    signals = []
    if cfg.exists():
        signals.append(f"path:{cfg}")
    if legacy.exists():
        signals.append(f"path:{legacy}")
    if not signals:
        return None
    return Detection(
        client="tokenpak-config",
        confidence="high",
        signals=signals,
        missing_steps=[],
        next_commands=["tokenpak status"],
    )


DETECTORS = [
    detect_anthropic,
    detect_openai_codex,
    detect_claude_code,
    detect_cursor,
    detect_aider,
    detect_proxy_config,
]


def detect_all(home: Optional[Path] = None) -> list[Detection]:
    found = []
    for fn in DETECTORS:
        d = fn(home=home)
        if d is not None:
            found.append(d)
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument(
        "--home",
        default=None,
        help="override $HOME for testing (do not use in production)",
    )
    args = parser.parse_args()

    home = Path(args.home) if args.home else None
    detections = detect_all(home=home)

    if args.json:
        json.dump(
            {
                "detected_count": len(detections),
                "detections": [asdict(d) for d in detections],
            },
            sys.stdout,
            indent=2,
        )
        sys.stdout.write("\n")
        return 0

    if not detections:
        print("No LLM client signals detected in this environment.")
        print("Hint: set ANTHROPIC_API_KEY or OPENAI_API_KEY before running again,")
        print("or run `tokenpak integrate` to wire a known client.")
        return 0

    print(f"Detected {len(detections)} client signal(s):\n")
    for d in detections:
        print(f"  • {d.client}  [{d.confidence}]")
        for s in d.signals:
            print(f"      signal: {s}")
        for m in d.missing_steps:
            print(f"      missing: {m}")
        if d.next_commands:
            print("      next:")
            for c in d.next_commands:
                print(f"        $ {c}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
