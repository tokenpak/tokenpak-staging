# SPDX-License-Identifier: Apache-2.0
"""CLI handlers for `tokenpak license`, `tokenpak plan`, `tokenpak activate`,
`tokenpak deactivate`. Free-tier today; Pro/Team/Enterprise surface ready.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path
from typing import Any

from tokenpak import licensing as _lic
from tokenpak.cli.commands.upgrade import DEFAULT_UPGRADE_URL


def _render_summary(s: dict[str, Any]) -> str:
    lines = [""]
    lines.append(f"  TOKENPAK license — {s['tier_label']}")
    lines.append("  " + "─" * 40)
    lines.append(f"  Tier      {s['tier_label']}")
    lines.append(f"  Status    {s['status']}")
    if s.get("email"):
        lines.append(f"  Email     {s['email']}")
    if s.get("activated_at"):
        lines.append(f"  Activated {s['activated_at']}")
    if s.get("expires_at"):
        lines.append(f"  Expires   {s['expires_at']}")
    if s.get("has_key"):
        lines.append(f"  Key       stored ({s['license_path']})")
    else:
        lines.append("  Key       (none — Free tier)")
    lines.append("")
    lines.append(
        f"  Gated features enabled: {s['enabled_gated_count']} / "
        f"{s['gated_feature_count']}"
    )
    lines.append("")
    if s["tier"] == _lic.TIER_FREE:
        lines.append("  You are on the Free tier. All Free-tier features are available.")
        lines.append(f"  Upgrade path: {DEFAULT_UPGRADE_URL}")
        lines.append("")
    elif s["status"] == "pending_validation":
        lines.append(
            "  ⏳ Pending validation — license key is stored but the validator "
            "is not yet live.\n"
            "     Free-tier features remain active in the meantime."
        )
        lines.append("")
    return "\n".join(lines)


def run_license(args: argparse.Namespace) -> int:
    """`tokenpak license` — show current license state."""
    s = _lic.summary_for_cli()
    if getattr(args, "as_json", False) or getattr(args, "json", False):
        print(json.dumps(s, indent=2))
        return 0
    print(_render_summary(s))
    return 0


def run_plan(args: argparse.Namespace) -> int:
    """`tokenpak plan` — show available plans + what the user has today.

    Catalog is discovered dynamically from the gate table + an optional
    ``<TOKENPAK_HOME>/pricing.json`` file. No hardcoded list, no
    misleading ``"TBD"`` strings.
    """
    s = _lic.summary_for_cli()
    plans = _lic.discover_plans()
    if getattr(args, "as_json", False) or getattr(args, "json", False):
        print(json.dumps({"current": s, "plans": plans}, indent=2))
        return 0
    print("")
    print("  TOKENPAK plans")
    print("  " + "─" * 40)
    print(f"  You are on:  {s['tier_label']}  (status: {s['status']})")
    print("")
    print("  Available plans:")
    for plan in plans:
        price = plan["price"]
        suffix = "" if price not in ("unannounced", "") else "  — pricing not yet announced"
        print(f"    {plan['label']:<11}  {price:<10}  ({plan['feature_count']} gated features){suffix}")
        if plan["blurb"]:
            print(f"               {plan['blurb']}")
    print("")
    print("  Use:")
    print("    tokenpak features            see every feature + entitlement state")
    print("    tokenpak activate --key-file PATH")
    print("")
    return 0


class _ActivationInputError(ValueError):
    pass


def _read_activation_key(args: argparse.Namespace) -> str:
    positional_key = (getattr(args, "key", "") or "").strip()
    key_file = getattr(args, "key_file", None)
    use_stdin = bool(getattr(args, "key_stdin", False))
    use_prompt = bool(getattr(args, "prompt_key", False))

    source_count = sum(bool(v) for v in (positional_key, key_file, use_stdin, use_prompt))
    if source_count > 1:
        raise _ActivationInputError(
            "activate: choose only one key source "
            "(--key-file, --key-stdin, --prompt-key, or the legacy positional key)"
        )

    if key_file:
        try:
            return Path(key_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise _ActivationInputError(
                f"activate: could not read --key-file {key_file!r}: {exc}"
            ) from exc
    if use_stdin:
        return sys.stdin.read().strip()
    if positional_key:
        return positional_key
    if use_prompt or sys.stdin.isatty():
        return getpass.getpass("License key: ").strip()

    raise _ActivationInputError(
        "activate: provide a license key via --key-file, --key-stdin, "
        "--prompt-key, or the legacy positional key"
    )


def run_activate(args: argparse.Namespace) -> int:
    """`tokenpak activate` — store a license key.

    Per Beta 1 hardening (Packet G), this rejects obviously invalid
    inputs (empty, too short, wrong charset, placeholder strings) and
    surfaces a non-zero exit so scripts / CI don't silently treat a
    bad activation as success.
    """
    try:
        key = _read_activation_key(args)
    except _ActivationInputError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)
    email = (getattr(args, "email", "") or "").strip()
    if not key:
        print(
            "activate: provide a non-empty license key via --key-file, "
            "--key-stdin, --prompt-key, or the legacy positional key",
            file=sys.stderr,
        )
        sys.exit(2)
    result = _lic.activate(key, email=email)
    if not result.ok:
        print(f"✖ activate failed: {result.summary}", file=sys.stderr)
        if result.error:
            print(f"  detail: {result.error}", file=sys.stderr)
        sys.exit(1)
    print("")
    print(f"  ✅ {result.summary}")
    if result.license and result.license.activated_at:
        print(f"  stored at: {_lic._license_path()}")
        print(f"  activated: {result.license.activated_at}")
    print("")
    return 0


def run_deactivate(args: argparse.Namespace) -> int:
    """`tokenpak deactivate` — revert to Free."""
    removed = _lic.deactivate()
    if removed:
        print("  ✅ License removed. Reverted to Free.")
    else:
        print("  (no license was installed — already on Free)")
    return 0
