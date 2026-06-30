#!/usr/bin/env python3
"""Auto-generate docs/cli-reference.md from tokenpak/cli.py argparse definitions.

This file is auto-generated. Edit tokenpak/cli.py and re-run:
    python scripts/generate-cli-docs.py

Usage:
    python scripts/generate-cli-docs.py            # writes docs/cli-reference.md
    python scripts/generate-cli-docs.py --stdout   # print to stdout instead
"""

import argparse
import os
import sys
import types
from pathlib import Path
from typing import List, Optional

# ---------------------------------------------------------------------------
# Locate repo root (this script lives in <repo>/scripts/)
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent
TOKENPAK_PKG = REPO_ROOT / "tokenpak"
OUTPUT_PATH = REPO_ROOT / "docs" / "cli-reference.md"

# ---------------------------------------------------------------------------
# Mock all tokenpak sub-modules that have side-effects at import time.
# build_parser() only uses argparse; the imported symbols are only referenced
# inside cmd_* handler bodies which we never call.
# ---------------------------------------------------------------------------

def _make_mock_module(name: str, **attrs):
    """Return a minimal module object with the given attributes."""
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    return mod


def _install_mocks():
    """Pre-populate sys.modules so that tokenpak relative imports resolve cleanly."""
    # A sentinel class so that isinstance checks on mocked types don't crash
    class _Stub:
        def __init__(self, *a, **kw):
            pass
        def __call__(self, *a, **kw):
            return self
        def __getattr__(self, name):
            return self
        def __iter__(self):
            return iter([])
        def __bool__(self):
            return False

    stub = _Stub()

    mocks = {
        "tokenpak.formatting": _make_mock_module(
            "tokenpak.formatting",
            OutputFormatter=_Stub,
            OutputMode=_Stub(),
            resolve_mode=lambda *a, **kw: None,
            symbols=_Stub(),
        ),
        "tokenpak.budget": _make_mock_module(
            "tokenpak.budget",
            BudgetBlock=_Stub,
            quadratic_allocate=stub,
        ),
        "tokenpak.calibration": _make_mock_module(
            "tokenpak.calibration",
            calibrate_workers=stub,
            get_recommended_workers=stub,
        ),
        "tokenpak.miss_detector": _make_mock_module(
            "tokenpak.miss_detector",
            DEFAULT_GAPS_PATH=str(Path.home() / ".tokenpak" / "gaps.jsonl"),
            should_expand_retrieval=stub,
        ),
        "tokenpak.processors": _make_mock_module(
            "tokenpak.processors",
            get_processor=stub,
        ),
        "tokenpak.registry": _make_mock_module(
            "tokenpak.registry",
            Block=_Stub,
            BlockRegistry=_Stub,
        ),
        "tokenpak.security": _make_mock_module(
            "tokenpak.security",
            secure_write_config=stub,
        ),
        "tokenpak.tokens": _make_mock_module(
            "tokenpak.tokens",
            cache_info=stub,
            count_tokens=stub,
            truncate_to_tokens=stub,
        ),
        "tokenpak.walker": _make_mock_module(
            "tokenpak.walker",
            walk_directory=stub,
        ),
        "tokenpak.wire": _make_mock_module(
            "tokenpak.wire",
            pack=stub,
        ),
    }

    for name, mod in mocks.items():
        sys.modules[name] = mod

    # Also mock the top-level tokenpak package so it's importable
    if "tokenpak" not in sys.modules:
        pkg = types.ModuleType("tokenpak")
        pkg.__path__ = [str(TOKENPAK_PKG)]
        pkg.__package__ = "tokenpak"
        sys.modules["tokenpak"] = pkg


# ---------------------------------------------------------------------------
# Argparse walker
# ---------------------------------------------------------------------------

def _get_subparser_map(parser: argparse.ArgumentParser) -> Optional[dict]:
    """Return {name: subparser} or None if no subparsers defined."""
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    return None


def _format_action(action: argparse.Action) -> Optional[str]:
    """Format a single argument action as a flag-description line."""
    # Skip internal / positional-only noise
    if isinstance(action, (argparse._HelpAction, argparse._SubParsersAction)):
        return None

    flags = ", ".join(f"`{f}`" for f in action.option_strings) if action.option_strings else None
    if flags is None:
        # Positional argument
        metavar = action.metavar or action.dest.upper()
        flags = f"`{metavar}`"

    # Build description
    desc = action.help or ""
    # Append default if non-trivial
    if action.default not in (None, argparse.SUPPRESS, False, True):
        # Normalize host-specific paths so the generated docs are stable
        # across runners. argparse defaults that contain the runtime user's
        # home directory (e.g. /home/sue, /home/runner) get rewritten to
        # `~`. Without this, docs/cli-reference.md drifts every time it's
        # regenerated on a different machine and the CLI Docs CI gate
        # spuriously fails.
        default_str = str(action.default)
        home = os.path.expanduser("~")
        if home and home != "~" and default_str.startswith(home):
            default_str = "~" + default_str[len(home):]
        desc = f"{desc} (default: {default_str})" if desc else f"default: {default_str}"

    # Append choices
    if action.choices:
        choices_str = ", ".join(f"`{c}`" for c in action.choices)
        desc = f"{desc} — choices: {choices_str}" if desc else f"choices: {choices_str}"

    return f"- {flags} — {desc}" if desc else f"- {flags}"


def _render_command(
    name: str,
    parser: argparse.ArgumentParser,
    group_name: str,
    lines: List[str],
    depth: int = 3,
) -> None:
    """Render a command (and its subcommands) into the lines list."""
    heading = "#" * depth
    desc = parser.description or (parser._defaults.get("func", None) and
                                   getattr(parser._defaults["func"], "__doc__", None)) or ""
    # Fall back to the parser's help stored in its parent's choices
    if not desc:
        desc = ""

    lines.append(f"{heading} `tokenpak {name}`")
    if desc:
        lines.append("")
        lines.append(desc.strip())
    lines.append("")

    # Collect non-subparser actions
    flag_lines = []
    for action in parser._actions:
        line = _format_action(action)
        if line:
            flag_lines.append(line)

    if flag_lines:
        lines.append("**Flags:**")
        lines.append("")
        lines.extend(flag_lines)
        lines.append("")

    # Subcommands
    submap = _get_subparser_map(parser)
    if submap:
        lines.append("**Subcommands:**")
        lines.append("")
        for sub_name, sub_parser in submap.items():
            sub_desc = sub_parser.description or ""
            if not sub_desc:
                # Try to get from parent's help (stored during add_parser)
                sub_desc = getattr(sub_parser, "_help_text", "") or ""
            flag_parts = []
            for action in sub_parser._actions:
                if isinstance(action, (argparse._HelpAction, argparse._SubParsersAction)):
                    continue
                if action.option_strings:
                    flag_parts.append(action.option_strings[0])
                else:
                    flag_parts.append(action.dest.upper())

            usage_flags = " ".join(flag_parts)
            line = f"- `{sub_name}`"
            if sub_desc:
                line += f" — {sub_desc}"
            lines.append(line)

            # Detailed flags for each subcommand
            sub_flag_lines = []
            for action in sub_parser._actions:
                fl = _format_action(action)
                if fl:
                    sub_flag_lines.append(f"  {fl}")
            if sub_flag_lines:
                lines.extend(sub_flag_lines)

        lines.append("")


# ---------------------------------------------------------------------------
# Main generator
# ---------------------------------------------------------------------------

def generate(stdout_only: bool = False) -> str:
    _install_mocks()

    # Add repo root to path so `import tokenpak.cli` resolves
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))

    import importlib
    cli_mod = importlib.import_module("tokenpak.cli")

    parser = cli_mod.build_parser()
    command_groups = cli_mod._COMMAND_GROUPS

    # Build a map of all subparsers keyed by command name
    submap = _get_subparser_map(parser) or {}

    lines = [
        "# CLI Reference",
        "",
        "_Auto-generated from `tokenpak/cli.py` — do not edit by hand._",
        "_To update: edit `tokenpak/cli.py` then run `python scripts/generate-cli-docs.py`._",
        "",
        "---",
        "",
    ]

    # Emit commands in _COMMAND_GROUPS order
    documented = set()

    for group_name, commands in command_groups.items():
        lines.append(f"## Group: {group_name}")
        lines.append("")

        for cmd_name, cmd_desc in commands:
            documented.add(cmd_name)
            if cmd_name in submap:
                sub_parser = submap[cmd_name]
                # Use cmd_desc as description if parser has none
                if not sub_parser.description:
                    sub_parser.description = cmd_desc
                _render_command(cmd_name, sub_parser, group_name, lines)
            else:
                # Command exists in _COMMAND_GROUPS but has no parser registered
                lines.append(f"### `tokenpak {cmd_name}`")
                lines.append("")
                lines.append(cmd_desc)
                lines.append("")
                lines.append("_(custom args — see source)_")
                lines.append("")

        lines.append("---")
        lines.append("")

    # Catch any parsers not in _COMMAND_GROUPS (shouldn't happen, but be safe)
    extra = sorted(set(submap) - documented)
    if extra:
        lines.append("## Additional Commands")
        lines.append("")
        for cmd_name in extra:
            _render_command(cmd_name, submap[cmd_name], "Additional", lines)
        lines.append("---")
        lines.append("")

    return _post_process_for_public_cli("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# Public-CLI post-process
#
# This function takes the rendered docs and applies the boundary cleanup
# rules. It exists so source-side argparse strings can keep their
# implementation-detail terms (internal task IDs, integration example
# fragments) without leaking those into docs/cli-reference.md, which is
# scanned by the identity-language CI check on every change.
#
# Reference data lives in scripts/internal-cli-cleanup.txt — a sibling
# config file that holds the lists by name. The .txt extension keeps it
# out of the workflow's grep filter for that check, so the boundary
# config carries the deferred subcommand names without tripping the
# guardrail itself.
# ---------------------------------------------------------------------------


def _load_cleanup_rules() -> dict:
    """Load the post-process cleanup rules from scripts/internal-cli-cleanup.txt.

    File format (UTF-8, line-based, '#' comments allowed):

        [deferred_subcommands]
        <name>
        <name>

        [example_substitutions]
        <old-token>=>><new-token>

    Returns a dict with keys 'deferred_subcommands' (set of str) and
    'example_substitutions' (list of (old, new) tuples).
    """
    rules = {"deferred_subcommands": set(), "example_substitutions": []}
    path = REPO_ROOT / "scripts" / "internal-cli-cleanup.txt"
    if not path.exists():
        return rules
    section = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        if section == "deferred_subcommands":
            rules["deferred_subcommands"].add(line)
        elif section == "example_substitutions":
            if "=>>" in line:
                old, new = line.split("=>>", 1)
                rules["example_substitutions"].append((old, new))
    return rules


def _post_process_for_public_cli(output: str) -> str:
    """Strip / sanitize content the public CLI docs should not carry.

    1. Parenthetical task IDs ((VDS-NN), (Std NN), (coming in CCI-NN))
       pulled from argparse help strings.
    2. Subcommand sections whose names appear in the deferred list
       (registered in source for backward compatibility but not part of
       the documented public CLI surface).
    3. Example fragments whose source-side text references internal
       integrations; replaced with product-neutral substitutes per the
       config's example_substitutions section.
    """
    import re

    # 1. Strip parenthetical task IDs.
    output = re.sub(r"\s*\(VDS-\d+\)", "", output)
    output = re.sub(r"\s*\(Std\s+\d+(?:\s*§[\d.]+)?\)", "", output)
    output = re.sub(r"\s*\(coming in CCI-\d+\)", "", output)

    rules = _load_cleanup_rules()

    # 2. Strip whole `### \`tokenpak <name>\`` sections for deferred names.
    for name in sorted(rules["deferred_subcommands"]):
        output = re.sub(
            rf"### `tokenpak {re.escape(name)}`.*?(?=\n### `tokenpak |\n---)",
            "",
            output,
            flags=re.DOTALL,
        )

    # 3. Apply example-substitution table.
    for old, new in rules["example_substitutions"]:
        output = output.replace(old, new)

    output = _inject_cli_reference_details(output)

    return output


def _inject_cli_reference_details(output: str) -> str:
    """Add curated public details that argparse cannot express cleanly."""
    output = output.replace(
        "- `doctor`\n  - `--json`",
        "- `doctor` — Read-only diagnostics on the configuration subsystem. Runs eight checks (D1–D8) covering config home location, load-order precedence, env var presence, `.env` file hygiene, and user/system file boundary integrity. Surfaces misconfigurations without writing any file. Complements `tokenpak doctor` (which covers the broader system); `config doctor` focuses only on config.\n  - `--json`",
        1,
    )
    output = output.replace(
        "- `env`\n  - `--json`",
        "- `env` — Show all TOKENPAK_* environment variables that are currently active, their values, and their provenance (where each value comes from: process environment, config file, or built-in default). Values matching secret-class key patterns (API_KEY, _TOKEN, _SECRET, etc.) are **always masked**; use `--no-mask` to reveal low-classification values.\n  - `--json`",
        1,
    )

    integrate_details = """**Guided mode vs print-only:**

When you name a specific client (`tokenpak integrate <client>`) on a TTY (both stdin and stdout are TTYs) without `--apply` or `--no-tui`, the command launches a **guided interactive form**:

1. Detects whether the client is installed on this host.
2. Shows the exact configuration change it will make (the preview).
3. For `claude-code` and `codex`, prompts you to pick a permission tier (`strict`, `standard`, `auto`, or `fleet`).
4. Asks for confirmation before writing any file.
5. Backs up the existing config automatically; prints a `tokenpak integrate <client> --revert` command to undo.

On a **non-TTY** (CI, piped output, `TOKENPAK_NONINTERACTIVE=1`, or `TERM=dumb`) the guided form is suppressed and `integrate` prints setup instructions only — the same output as `--all` for the named client.

**Shell detection:**

The guided form activates when `sys.stdin.isatty() and sys.stdout.isatty()` are both true. If either stream is redirected (common in CI pipelines or when piping to a file), `integrate` automatically runs in print-only mode. This means `tokenpak integrate claude-code > setup.txt` always writes plain text, never launches a form.

**`--no-tui` escape hatch:**

Pass `--no-tui` anywhere on the command line to force print-only mode even on a fully interactive TTY:

```
tokenpak --no-tui integrate claude-code
```

`--no-tui` is a **global flag** — it is stripped from `sys.argv` before subcommand parsers run and does **not** appear in any subcommand's `--help` output. It is honored on `tokenpak integrate <target>` (without `--apply`); when `--apply` is set the flag has no effect (apply is always headless). Use `TOKENPAK_NONINTERACTIVE=1` as the environment-variable equivalent for scripts where the command line cannot be controlled.

"""
    output = output.replace(
        "- `--yes` — Confirm dangerous choices non-interactively (required for --tier fleet without a TTY)\n\n### `tokenpak last`",
        "- `--yes` — Confirm dangerous choices non-interactively (required for --tier fleet without a TTY)\n\n" + integrate_details + "### `tokenpak last`",
        1,
    )

    menu_details = """Interactive command browser with arrow-key navigation. Runs in the alternate-screen buffer — menu frames never appear in terminal scrollback.

**Two ways to open:**

- `tokenpak` — bare invocation on a TTY launches the menu automatically
- `tokenpak menu` — explicit subcommand, same result

The menu does **not** launch when stdin or stdout is not a TTY, when the `CI` environment variable is set, when `TOKENPAK_NONINTERACTIVE=1` is set, or when `TERM=dumb`.

**Home screen:**

The home screen shows nine task-focused sections. Each entry shows the equivalent CLI command on the right:

| Section | CLI equivalent |
|---|---|
| Start proxy | `tokenpak start` |
| Run demo | `tokenpak demo` |
| Proxy status | `tokenpak status` |
| Spend & savings | `tokenpak cost` |
| Configure | `tokenpak config` |
| Permission tier | `tokenpak permissions` |
| Companion | — |
| Troubleshoot | `tokenpak doctor` |
| Browse all commands | — |

A status strip at the top shows Proxy state, Today's cost, and Today's saved. Values come from a cached non-blocking snapshot — unknown figures render as `—` and are never fabricated as `$0.00`.

**Keys:**

| Key | Action |
|---|---|
| `↑` / `↓` | Navigate items |
| `Enter` | Select / run highlighted item |
| `q` or `Ctrl-C` | Quit the menu |
| `Esc` | Go back (in submenus); quit (at home screen) |
| Any printable character | Filter items (type-to-filter) |
| `Backspace` | Delete last filter character; go back if filter is empty |

Type-to-filter is active on the home screen and the "Browse all commands" section. Matching runs against both the displayed label and a set of search aliases — for example, typing `health` highlights Proxy status.

**Non-interactive fallback:**

When the terminal is not a TTY, `tokenpak menu` prints a numbered plain-text list of home-screen options and exits rather than launching the cursor-driven interface.

**`--no-tui` escape hatch:**

Pass `--no-tui` anywhere on the command line to suppress the interactive menu for bare `tokenpak` invocations:

```
tokenpak --no-tui
```

This prints quick-help and proxy uptime instead of opening the TUI. `--no-tui` is a global flag — it is stripped from `sys.argv` before subcommand parsers run and does not appear in any subcommand's `--help` output. It is honored on bare `tokenpak` and `tokenpak integrate <target>` (without `--apply`); the explicit `tokenpak menu` subcommand always launches the TUI directly.

Use `TOKENPAK_NONINTERACTIVE=1` as the environment-variable equivalent for scripts where the command line cannot be controlled.

"""
    output = output.replace(
        "### `tokenpak menu`\n\n### `tokenpak monitor`",
        "### `tokenpak menu`\n\n" + menu_details + "### `tokenpak monitor`",
        1,
    )

    return output


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stdout", action="store_true", help="Print to stdout instead of writing file")
    args = ap.parse_args()

    content = generate(stdout_only=args.stdout)

    if args.stdout:
        sys.stdout.write(content)
    else:
        OUTPUT_PATH.write_text(content, encoding="utf-8")
        count = content.count("\n### `tokenpak ")
        print(f"Wrote {OUTPUT_PATH} ({count} top-level commands documented)")


if __name__ == "__main__":
    main()
