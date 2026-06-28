# SPDX-License-Identifier: Apache-2.0
"""Codex usage observation and run-scoped exec capture.

This module is intentionally outside the TokenPak provider adapter path.  It
observes Codex-native usage surfaces and writes only run-scoped sidecars for
TokenPak-launched ``codex exec --json`` runs.  It does not mutate
``monitor.db`` or Dispatch receipts.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import secrets
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

from tokenpak import __version__ as TOKENPAK_VERSION
from tokenpak import _paths

USAGE_SCHEMA_VERSION = "codex-usage.v1"

FORBIDDEN_BASENAMES = {
    "auth.json",
    "history.jsonl",
    "config.toml",
    "codex-tui.log",
}
FORBIDDEN_SUFFIXES = {".log"}

USAGE_KEYS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)


class UsageSafetyError(ValueError):
    """Raised when a candidate usage source is forbidden before content read."""


@dataclass(frozen=True)
class SessionSelection:
    path: Path | None
    run_scope: str
    warnings: list[dict[str, Any]]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _json_dumps(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


def _stable_hmac(salt: bytes, text: str) -> str:
    return hmac.new(salt, text.encode("utf-8"), hashlib.sha256).hexdigest()


def salt_path() -> Path:
    return _paths.under("companion", "codex_usage_salt")


def load_or_create_salt(path: Path | None = None) -> bytes:
    """Load the local HMAC salt, creating it with user-private permissions."""

    target = path or salt_path()
    if target.exists():
        return target.read_bytes()
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    salt = secrets.token_bytes(32)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(target, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(salt)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    return salt


def _check_forbidden_segments(path: Path) -> None:
    parts = {part.lower() for part in path.parts}
    forbidden = sorted(parts & FORBIDDEN_BASENAMES)
    if forbidden:
        raise UsageSafetyError(f"refusing forbidden Codex file: {forbidden[0]}")
    if path.name.lower() in FORBIDDEN_BASENAMES:
        raise UsageSafetyError(f"refusing forbidden Codex file: {path.name}")
    if path.suffix.lower() in FORBIDDEN_SUFFIXES:
        raise UsageSafetyError(f"refusing raw Codex log file: {path.name}")


def validate_session_path(path: str | os.PathLike[str]) -> Path:
    """Validate a session JSONL path before opening it.

    The original path and symlink target are both checked.  This lets a caller
    pass an explicit session path while preventing symlink escapes into
    credential/config/history/log files.
    """

    raw = Path(path).expanduser()
    _check_forbidden_segments(raw)
    try:
        resolved = raw.resolve(strict=True)
    except FileNotFoundError as exc:
        raise UsageSafetyError(f"session file not found: {raw}") from exc
    _check_forbidden_segments(resolved)
    if not resolved.is_file():
        raise UsageSafetyError(f"session source is not a file: {raw}")
    if resolved.suffix.lower() != ".jsonl":
        raise UsageSafetyError(f"session source must be .jsonl: {raw.name}")
    return resolved


def resolve_codex_home(codex_home: str | os.PathLike[str] | None = None) -> Path:
    if codex_home:
        return Path(codex_home).expanduser()
    env_home = os.environ.get("CODEX_HOME", "").strip()
    if env_home:
        return Path(env_home).expanduser()
    return Path.home() / ".codex"


def candidate_session_files(codex_home: str | os.PathLike[str] | None = None) -> list[Path]:
    home = resolve_codex_home(codex_home)
    root = home / "sessions"
    if not root.exists():
        return []
    candidates: list[Path] = []
    for path in root.rglob("*.jsonl"):
        try:
            candidates.append(validate_session_path(path))
        except UsageSafetyError:
            continue
    return sorted(candidates, key=lambda p: p.stat().st_mtime_ns, reverse=True)


def select_latest_session(
    codex_home: str | os.PathLike[str] | None = None,
    *,
    ambiguity_window_s: int = 300,
) -> SessionSelection:
    candidates = candidate_session_files(codex_home)
    if not candidates:
        return SessionSelection(
            path=None,
            run_scope="unknown",
            warnings=[{"code": "no_sessions", "message": "no Codex session JSONL files found"}],
        )
    newest = candidates[0]
    newest_mtime = newest.stat().st_mtime
    recent = [
        p for p in candidates
        if newest_mtime - p.stat().st_mtime <= ambiguity_window_s
    ]
    if len(recent) > 1:
        return SessionSelection(
            path=None,
            run_scope="ambiguous",
            warnings=[
                {
                    "code": "ambiguous_latest_session",
                    "message": "multiple Codex session files changed recently; pass --session",
                    "candidate_count": len(recent),
                }
            ],
        )
    return SessionSelection(path=newest, run_scope="inferred", warnings=[])


def _normalize_usage(raw: Any) -> dict[str, int]:
    source = raw if isinstance(raw, dict) else {}
    normalized: dict[str, int] = {}
    for key in USAGE_KEYS:
        value = source.get(key, 0)
        try:
            normalized[key] = int(value or 0)
        except (TypeError, ValueError):
            normalized[key] = 0
    if "total_tokens" not in source or source.get("total_tokens") is None:
        normalized["total_tokens"] = (
            normalized["input_tokens"] + normalized["output_tokens"]
        )
    return normalized


def _session_fingerprint(path: Path, salt: bytes) -> str:
    stat = path.stat()
    metadata = (
        f"codex-session-v1:"
        f"dev={stat.st_dev}:ino={stat.st_ino}:"
        f"mtime_ns={stat.st_mtime_ns}:size={stat.st_size}"
    )
    return "hmac-sha256:" + _stable_hmac(salt, metadata)


def _event_id(
    *,
    salt: bytes,
    source_fingerprint: str,
    event_ordinal: int,
    payload: dict[str, Any],
) -> str:
    material = _json_dumps(
        {
            "source_fingerprint": source_fingerprint,
            "event_ordinal": event_ordinal,
            "payload": payload,
        }
    )
    return "hmac-sha256:" + _stable_hmac(salt, material)


def _token_count_payload(obj: Any) -> dict[str, Any] | None:
    if not isinstance(obj, dict):
        return None
    if obj.get("type") != "event_msg":
        return None
    payload = obj.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "token_count":
        return None
    return payload


def parse_session_jsonl(
    path: str | os.PathLike[str],
    *,
    run_scope: str = "bounded",
    salt: bytes | None = None,
) -> dict[str, Any]:
    """Parse Codex TUI token-count events from a safe session JSONL file."""

    session_path = validate_session_path(path)
    local_salt = salt or load_or_create_salt()
    source_fingerprint = _session_fingerprint(session_path, local_salt)
    events: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    token_ordinal = 0

    with session_path.open("r", encoding="utf-8") as fh:
        for line_number, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            if '"token_count"' not in line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                warnings.append(
                    {
                        "code": "malformed_jsonl",
                        "line": line_number,
                        "message": "ignored malformed JSONL line",
                    }
                )
                continue
            payload = _token_count_payload(obj)
            if payload is None:
                continue
            token_ordinal += 1
            incremental = _normalize_usage(payload.get("last_token_usage"))
            cumulative = _normalize_usage(payload.get("total_token_usage"))
            hash_payload = {
                "incremental_model_call_usage": incremental,
                "cumulative_session_usage": cumulative,
                "model_context_window": payload.get("model_context_window"),
            }
            events.append(
                {
                    "provider": "codex",
                    "ingest_method": "session_jsonl",
                    "source_kind": "codex_session_jsonl",
                    "usage_schema_version": USAGE_SCHEMA_VERSION,
                    "timestamp": obj.get("timestamp") or payload.get("timestamp"),
                    "observed_at": _utc_now(),
                    "run_scope": run_scope,
                    "claim_eligibility": "spend_only",
                    "pricing": {
                        "known": False,
                        "reason": "unknown_billing_context",
                    },
                    "event_ordinal": token_ordinal,
                    "source_line": line_number,
                    "source_fingerprint": source_fingerprint,
                    "source_event_id": _event_id(
                        salt=local_salt,
                        source_fingerprint=source_fingerprint,
                        event_ordinal=token_ordinal,
                        payload=hash_payload,
                    ),
                    "model": payload.get("model"),
                    "incremental_model_call_usage": incremental,
                    "cumulative_session_usage": cumulative,
                    "model_context_window": payload.get("model_context_window"),
                    "rate_limits_present": bool(payload.get("rate_limits")),
                }
            )

    if not events:
        warnings.append(
            {
                "code": "no_token_count_events",
                "message": "no Codex token_count events found",
            }
        )
    return {
        "ok": True,
        "provider": "codex",
        "source_kind": "codex_session_jsonl",
        "usage_schema_version": USAGE_SCHEMA_VERSION,
        "run_scope": run_scope,
        "claim_eligibility": "spend_only",
        "pricing": {
            "known": False,
            "reason": "unknown_billing_context",
        },
        "source_fingerprint": source_fingerprint,
        "event_count": len(events),
        "events": events,
        "warnings": warnings,
    }


def parse_latest_session(
    codex_home: str | os.PathLike[str] | None = None,
    *,
    salt: bytes | None = None,
) -> dict[str, Any]:
    selection = select_latest_session(codex_home)
    if selection.path is None:
        return {
            "ok": False,
            "provider": "codex",
            "source_kind": "codex_session_jsonl",
            "usage_schema_version": USAGE_SCHEMA_VERSION,
            "run_scope": selection.run_scope,
            "event_count": 0,
            "events": [],
            "warnings": selection.warnings,
        }
    result = parse_session_jsonl(selection.path, run_scope=selection.run_scope, salt=salt)
    result["warnings"] = [*selection.warnings, *result.get("warnings", [])]
    return result


def _print_json(data: dict[str, Any], stream: TextIO = sys.stdout) -> None:
    print(json.dumps(data, indent=2, sort_keys=True), file=stream)


def codex_cli_version() -> str:
    try:
        result = subprocess.run(
            ["codex", "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return "unknown"
    value = (result.stdout or result.stderr).strip().splitlines()
    return value[0] if value else "unknown"


def _exec_usage_event(
    usage: Any,
    *,
    salt: bytes,
    run_id: str,
    event_ordinal: int,
) -> dict[str, Any]:
    incremental = _normalize_usage(usage)
    source_fingerprint = "hmac-sha256:" + _stable_hmac(salt, f"codex-exec:{run_id}")
    payload = {"incremental_model_call_usage": incremental}
    return {
        "provider": "codex",
        "ingest_method": "exec_json",
        "source_kind": "codex_exec_json",
        "usage_schema_version": USAGE_SCHEMA_VERSION,
        "timestamp": None,
        "observed_at": _utc_now(),
        "run_scope": "proven",
        "claim_eligibility": "spend_only",
        "pricing": {
            "known": False,
            "reason": "unknown_billing_context",
        },
        "event_ordinal": event_ordinal,
        "source_fingerprint": source_fingerprint,
        "source_event_id": _event_id(
            salt=salt,
            source_fingerprint=source_fingerprint,
            event_ordinal=event_ordinal,
            payload=payload,
        ),
        "incremental_model_call_usage": incremental,
    }


def _default_sidecar_dir() -> Path:
    return _paths.ensure_home() / "companion" / "codex-exec-usage"


def write_exec_sidecar(
    events: list[dict[str, Any]],
    *,
    run_id: str,
    sidecar: str | os.PathLike[str] | None = None,
    codex_version: str | None = None,
) -> Path:
    target = Path(sidecar).expanduser() if sidecar else _default_sidecar_dir() / f"{run_id}.json"
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    artifact = {
        "artifact_kind": "codex_exec_usage_sidecar",
        "usage_schema_version": USAGE_SCHEMA_VERSION,
        "provider": "codex",
        "source_kind": "codex_exec_json",
        "run_id": run_id,
        "run_scope": "proven",
        "claim_eligibility": "spend_only",
        "pricing": {
            "known": False,
            "reason": "unknown_billing_context",
        },
        "tokenpak_version": TOKENPAK_VERSION,
        "codex_cli_version": codex_version or codex_cli_version(),
        "command_redacted": True,
        "event_count": len(events),
        "events": events,
        "created_at": _utc_now(),
    }
    target.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    try:
        target.chmod(0o600)
    except OSError:
        pass
    return target


def _ensure_codex_exec_json_command(args: list[str]) -> list[str]:
    if args[:2] == ["codex", "exec"]:
        if "--json" in args:
            return args
        return [*args[:2], "--json", *args[2:]]
    if args and args[0] == "codex":
        return args
    return ["codex", "exec", "--json", *args]


def capture_codex_exec(
    command_args: list[str],
    *,
    sidecar: str | os.PathLike[str] | None = None,
    quiet_capture: bool = False,
    stdout: TextIO = sys.stdout,
    popen_factory: Any = subprocess.Popen,
    salt: bytes | None = None,
    codex_version: str | None = None,
) -> tuple[int, Path]:
    """Launch Codex exec JSONL, preserve output, and write a usage sidecar."""

    command = _ensure_codex_exec_json_command(command_args)
    run_id = str(uuid.uuid4())
    local_salt = salt or load_or_create_salt()
    events: list[dict[str, Any]] = []
    proc = popen_factory(
        command,
        stdout=subprocess.PIPE,
        stderr=None,
        text=True,
        bufsize=1,
    )
    assert proc.stdout is not None
    for raw_line in proc.stdout:
        if not quiet_capture:
            stdout.write(raw_line)
            stdout.flush()
        try:
            obj = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("type") == "turn.completed":
            usage = obj.get("usage")
            if usage is not None:
                events.append(
                    _exec_usage_event(
                        usage,
                        salt=local_salt,
                        run_id=run_id,
                        event_ordinal=len(events) + 1,
                    )
                )
    return_code = proc.wait()
    path = write_exec_sidecar(
        events,
        run_id=run_id,
        sidecar=sidecar,
        codex_version=codex_version,
    )
    return return_code, path


def main_usage(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tokenpak codex usage",
        description="Inspect Codex token usage without mutating TokenPak ledgers.",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--session", metavar="PATH", help="Codex session JSONL file")
    source.add_argument("--latest", action="store_true", help="Parse the latest unambiguous session")
    parser.add_argument("--codex-home", metavar="PATH", help="Codex home for --latest")
    parser.add_argument("--json", action="store_true", dest="as_json", help="Emit JSON")
    args = parser.parse_args(argv)

    try:
        if args.session:
            result = parse_session_jsonl(args.session, run_scope="bounded")
        else:
            result = parse_latest_session(args.codex_home)
    except UsageSafetyError as exc:
        result = {
            "ok": False,
            "provider": "codex",
            "source_kind": "codex_session_jsonl",
            "usage_schema_version": USAGE_SCHEMA_VERSION,
            "run_scope": "unknown",
            "event_count": 0,
            "events": [],
            "warnings": [{"code": "forbidden_source", "message": str(exc)}],
        }
    if args.as_json:
        _print_json(result)
    else:
        print(f"Codex usage events: {result.get('event_count', 0)}")
        print(f"run_scope: {result.get('run_scope', 'unknown')}")
        for warning in result.get("warnings", []):
            print(f"warning: {warning.get('code')}: {warning.get('message')}")
    return 0 if result.get("ok") else 2


def main_exec_capture(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tokenpak codex exec",
        description="Run codex exec --json and capture run-scoped usage sidecar.",
    )
    parser.add_argument("--capture", action="store_true", help="Capture usage sidecar")
    parser.add_argument("--sidecar", metavar="PATH", help="Write sidecar to PATH")
    parser.add_argument("--quiet-capture", action="store_true", help="Do not pass through JSONL stdout")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="Command after --, or codex exec args")
    args = parser.parse_args(argv)
    if not args.capture:
        parser.error("--capture is required for tokenpak codex exec interception")
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("missing codex exec command or prompt")
    return_code, path = capture_codex_exec(
        command,
        sidecar=args.sidecar,
        quiet_capture=args.quiet_capture,
    )
    print(f"\n[tokenpak] Codex usage sidecar: {path}", file=sys.stderr)
    return return_code


__all__ = [
    "USAGE_SCHEMA_VERSION",
    "UsageSafetyError",
    "candidate_session_files",
    "capture_codex_exec",
    "load_or_create_salt",
    "main_exec_capture",
    "main_usage",
    "parse_latest_session",
    "parse_session_jsonl",
    "select_latest_session",
    "validate_session_path",
    "write_exec_sidecar",
]
