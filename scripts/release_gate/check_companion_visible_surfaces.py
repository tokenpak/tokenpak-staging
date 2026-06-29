#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Guard companion visible-surface files against orphaned references.

The check is intentionally narrow: it verifies that shipped companion title and
statusline surfaces keep their source files, hook references resolve, and bytecode
artifacts do not survive without matching source. It does not restore or enable
new companion UI behavior.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

REQUIRED_FILES = (
    Path("tokenpak/companion/hooks/pre_send.sh"),
    Path("tokenpak/companion/hooks/session_start_name.sh"),
    Path("tokenpak/companion/statusline/pakline.sh"),
    Path("tokenpak/companion/codex/statusline_config.py"),
    Path("tokenpak/companion/codex/state_lock.py"),
)

REQUIRED_TEXT_MARKERS = {
    Path("tokenpak/companion/hooks/pre_send.sh"): (
        "hookSpecificOutput",
        "sessionTitle",
        "titles",
    ),
    Path("tokenpak/companion/statusline/pakline.sh"): (
        "total_cost_usd",
        "exceeds_200k_tokens",
        "total_duration_ms",
    ),
    Path("tokenpak/companion/codex/statusline_config.py"): (
        "thread-title",
        "context-remaining",
        "context-used",
        "context-window-size",
        "task-progress",
    ),
}

_SKIP_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
}

_CODEX_PACKAGE_ATTRS = {"launch"}
_LITERAL_REFERENCE_FIXTURES = {
    Path("tests/release_gate/test_companion_visible_surfaces.py"),
}


@dataclass(frozen=True)
class Finding:
    path: Path
    message: str

    def format(self) -> str:
        return f"{self.path}: {self.message}"


def _is_skipped(path: Path) -> bool:
    return any(part in _SKIP_DIRS for part in path.parts)


def _git_files(root: Path) -> list[Path]:
    try:
        proc = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=root,
            check=True,
            capture_output=True,
        )
    except Exception:
        return []
    return [Path(p.decode()) for p in proc.stdout.split(b"\0") if p]


def _iter_files(root: Path) -> list[Path]:
    tracked = _git_files(root)
    if tracked:
        return tracked
    files: list[Path] = []
    for path in root.rglob("*"):
        rel = path.relative_to(root)
        if path.is_file() and not _is_skipped(rel):
            files.append(rel)
    return files


def _pyc_source_path(pyc: Path) -> Path:
    if pyc.parent.name == "__pycache__":
        module = pyc.name.split(".", 1)[0]
        return pyc.parent.parent / f"{module}.py"
    return pyc.with_suffix(".py")


def check_required_files(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for rel in REQUIRED_FILES:
        if not (root / rel).is_file():
            findings.append(Finding(rel, "required companion visible-surface source is missing"))
    return findings


def check_required_text_markers(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for rel, markers in REQUIRED_TEXT_MARKERS.items():
        path = root / rel
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for marker in markers:
            if marker not in text:
                findings.append(Finding(rel, f"missing required marker {marker!r}"))
    return findings


def check_pyc_orphans(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    companion_root = root / "tokenpak" / "companion"
    if not companion_root.exists():
        return findings
    for pyc in companion_root.rglob("*.pyc"):
        rel = pyc.relative_to(root)
        if _is_skipped(rel):
            continue
        source = _pyc_source_path(pyc)
        if not source.is_file():
            findings.append(Finding(rel, f"bytecode has no matching source {source.relative_to(root)}"))
    return findings


def _iter_hook_commands(payload: object) -> Iterable[str]:
    if not isinstance(payload, dict):
        return
    hooks = payload.get("hooks")
    if not isinstance(hooks, dict):
        return
    for groups in hooks.values():
        if not isinstance(groups, list):
            continue
        for group in groups:
            if isinstance(group, dict):
                commands = group.get("hooks", [])
            elif isinstance(group, list):
                commands = group
            else:
                continue
            for entry in commands:
                if isinstance(entry, dict) and isinstance(entry.get("command"), str):
                    yield entry["command"]


def _module_to_path(module: str) -> Path | None:
    if not module.startswith(("tokenpak.", "scripts.")):
        return None
    return Path(*module.split(".")).with_suffix(".py")


def _command_references(command: str) -> Iterable[Path]:
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    for i, token in enumerate(tokens):
        if token == "-m" and i + 1 < len(tokens):
            ref = _module_to_path(tokens[i + 1])
            if ref is not None:
                yield ref
        if token.endswith((".py", ".sh")):
            path = Path(token)
            if not path.is_absolute() and path.parts and path.parts[0] in {"tokenpak", "scripts"}:
                yield path


def check_hooks_json_references(root: Path, files: Iterable[Path]) -> list[Finding]:
    findings: list[Finding] = []
    for rel in files:
        if rel.name != "hooks.json":
            continue
        try:
            payload = json.loads((root / rel).read_text(encoding="utf-8"))
        except Exception as exc:
            findings.append(Finding(rel, f"hooks.json is not valid JSON: {exc}"))
            continue
        for command in _iter_hook_commands(payload):
            for ref in _command_references(command):
                if not (root / ref).is_file():
                    findings.append(Finding(rel, f"hook command references missing file/module: {ref}"))
    return findings


def check_literal_module_references(root: Path, files: Iterable[Path]) -> list[Finding]:
    findings: list[Finding] = []
    module_re = re.compile(r"tokenpak\.companion\.codex\.([A-Za-z_][A-Za-z0-9_]*)")
    for rel in files:
        if rel in _LITERAL_REFERENCE_FIXTURES:
            continue
        if rel.suffix not in {".py", ".md", ".rst", ".toml", ".json", ".yaml", ".yml", ".sh"}:
            continue
        text = (root / rel).read_text(encoding="utf-8", errors="replace")
        for match in module_re.finditer(text):
            module = match.group(1)
            if module in _CODEX_PACKAGE_ATTRS:
                continue
            source = Path("tokenpak/companion/codex") / f"{module}.py"
            if not (root / source).is_file():
                findings.append(Finding(rel, f"dangling companion codex module reference: {source}"))
    return findings


def run(root: Path) -> list[Finding]:
    root = root.resolve()
    files = _iter_files(root)
    findings: list[Finding] = []
    findings.extend(check_required_files(root))
    findings.extend(check_required_text_markers(root))
    findings.extend(check_pyc_orphans(root))
    findings.extend(check_hooks_json_references(root, files))
    findings.extend(check_literal_module_references(root, files))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="Repository root to scan")
    args = parser.parse_args(argv)

    findings = run(Path(args.root))
    if findings:
        print("companion visible-surface guard found unresolved references:", file=sys.stderr)
        for finding in findings:
            print(f"::error::{finding.format()}", file=sys.stderr)
        return 1
    print("companion visible-surface guard: ok", file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
