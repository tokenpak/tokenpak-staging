# SPDX-License-Identifier: Apache-2.0
"""``tokenpak cards`` CLI subcommand — Cards authoring layer (Std 54).

Markdown-simple to write, canonical-schema strict to run: ``.tip.md`` /
``.pak.md`` cards compile into canonical TokenPak contracts. The runtime
trusts only validated compiled JSON manifests — raw Markdown is never
executed.

Subcommands (Std 54 §L, Phase 1):
    discover               Find card source files in the current project
    validate [PATH]        Validate one card or all discovered cards
    compile  [PATH]        Compile to canonical JSON manifest(s)
    install  [PATH]        Compile + record in the installed manifest
    list                   Discovered + installed cards
    inspect <name>         Static declarations (no connector traffic)
    preview <name>         Static declared-scope dump (OSS) / --pro probe
    scaffold               New card skeleton in the project tree (§J)
    doctor                 Authoring-layer diagnostics

Flags: ``--type tip|pak|worker``, ``--mode dev|locked``, ``--pro``,
``--strict``. ``--type worker`` is accepted on the surface (§L) but is
Phase 2 — using it reports a clear not-yet-available error.

Boundary note (Std 54 invariant 13): ``tokenpak cards`` operates on
authoring sources; ``tokenpak pak`` operates on runtime Pak objects.
They are different artifacts and the verbs are NOT aliases.

Exit codes follow the ``tokenpak pak`` convention:
    0  success
    1  user-facing error (validation failure, missing file, ...)
    2  argparse usage error (handled by argparse itself)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Optional

# Tokenpak imports are deferred into handlers to keep `tokenpak --help`
# fast (the cards package pulls yaml + the tip contracts on import).


# ---------------------------------------------------------------------------
# Argparse builder — wired into _cli_core.build_parser via _build_cards_parser
# ---------------------------------------------------------------------------


def _add_type_flag(p: Any) -> None:
    p.add_argument(
        "--type",
        dest="card_type",
        choices=["tip", "pak", "worker"],
        help="Filter/select card type (worker is Phase 2 — not yet available)",
    )


def _add_mode_flags(p: Any, *, strict: bool = True) -> None:
    p.add_argument(
        "--mode",
        choices=["dev", "locked"],
        default="dev",
        help="Trust mode: dev (project discovery, warnings allowed) or "
        "locked (installed cards only; strict consistency)",
    )
    if strict:
        p.add_argument(
            "--strict",
            action="store_true",
            help="Require exact card == adapter capability equality",
        )


def _add_json_flag(p: Any) -> None:
    p.add_argument("--json", action="store_true", help="Emit JSON instead of text")


def build_cards_parser(sub: Any) -> None:
    """Register the ``tokenpak cards`` subcommand and its actions on ``sub``."""
    p_cards = sub.add_parser(
        "cards",
        help="Author, validate, compile Markdown cards (TIP/PAK authoring layer)",
        description=(
            "TokenPak Cards authoring layer: .tip.md / .pak.md Markdown "
            "cards compile into canonical TokenPak contracts. The runtime "
            "trusts only validated compiled manifests; raw Markdown is "
            "never executed. Note: `tokenpak cards` operates on authoring "
            "sources, `tokenpak pak` on runtime Pak objects — they are "
            "not aliases."
        ),
    )
    csub = p_cards.add_subparsers(dest="cards_action", required=False)

    p_discover = csub.add_parser("discover", help="Find card source files in the current project")
    _add_type_flag(p_discover)
    _add_mode_flags(p_discover, strict=False)
    _add_json_flag(p_discover)
    p_discover.set_defaults(func=cmd_cards_discover)

    p_validate = csub.add_parser(
        "validate", help="Validate one card (PATH) or all discovered cards"
    )
    p_validate.add_argument("path", nargs="?", help="Card file to validate (default: all)")
    _add_type_flag(p_validate)
    _add_mode_flags(p_validate)
    _add_json_flag(p_validate)
    p_validate.set_defaults(func=cmd_cards_validate)

    p_compile = csub.add_parser(
        "compile",
        help="Compile card(s) to canonical JSON manifests",
        description=(
            "Validates then compiles cards into canonical JSON manifests "
            "under .tokenpak/cache/cards/compiled/. Only validated "
            "compiled manifests are runtime inputs."
        ),
    )
    p_compile.add_argument("path", nargs="?", help="Card file to compile (default: all)")
    _add_type_flag(p_compile)
    _add_mode_flags(p_compile)
    _add_json_flag(p_compile)
    p_compile.set_defaults(func=cmd_cards_compile)

    p_install = csub.add_parser(
        "install",
        help="Compile + record card(s) in the project installed manifest",
    )
    p_install.add_argument("path", nargs="?", help="Card file to install (default: all)")
    _add_type_flag(p_install)
    _add_mode_flags(p_install)
    _add_json_flag(p_install)
    p_install.set_defaults(func=cmd_cards_install)

    p_list = csub.add_parser("list", help="List discovered + installed cards")
    _add_type_flag(p_list)
    _add_mode_flags(p_list, strict=False)
    _add_json_flag(p_list)
    p_list.set_defaults(func=cmd_cards_list)

    p_inspect = csub.add_parser(
        "inspect",
        help="Show a card's static declarations (no connector traffic)",
    )
    p_inspect.add_argument("name", help="Card name (frontmatter `name:`)")
    _add_type_flag(p_inspect)
    _add_mode_flags(p_inspect, strict=False)
    _add_json_flag(p_inspect)
    p_inspect.set_defaults(func=cmd_cards_inspect)

    p_preview = csub.add_parser(
        "preview",
        help="Static declared-scope preview (unranked; Pro Local adds scoring)",
        description=(
            "OSS preview is a static declared scope/filter dump — no "
            "scored recall, no hydration, no render-to-messages "
            "injection. Live unranked candidates additionally require a "
            "registered connector (Phase 2). --pro probes the Pro Local "
            "daemon and falls back to the static dump when absent."
        ),
    )
    p_preview.add_argument("name", help="Card name (frontmatter `name:`)")
    p_preview.add_argument(
        "--pro", action="store_true", help="Use Pro Local scoring when available"
    )
    p_preview.add_argument("--query", default=None, help="Recall query (used with --pro)")
    _add_mode_flags(p_preview, strict=False)
    _add_json_flag(p_preview)
    p_preview.set_defaults(func=cmd_cards_preview)

    p_scaffold = csub.add_parser(
        "scaffold",
        help="Create a new card skeleton in the project tree",
        description=(
            "Scaffolds into the project (integrations/<name>/ for tip "
            "cards, paks/ for pak cards) — NEVER into the installed "
            "package tree."
        ),
    )
    p_scaffold.add_argument(
        "--type",
        dest="card_type",
        choices=["tip", "pak", "worker"],
        required=True,
        help="Card type to scaffold (worker is Phase 2 — not yet available)",
    )
    p_scaffold.add_argument(
        "--kind",
        default="provider_adapter",
        help="tip_kind for tip cards (Phase 1: provider_adapter)",
    )
    p_scaffold.add_argument("--name", required=True, help="Card name (lowercase slug)")
    _add_json_flag(p_scaffold)
    p_scaffold.set_defaults(func=cmd_cards_scaffold)

    p_doctor = csub.add_parser("doctor", help="Cards authoring-layer diagnostics")
    _add_mode_flags(p_doctor, strict=False)
    _add_json_flag(p_doctor)
    p_doctor.set_defaults(func=cmd_cards_doctor)

    # Default — bare `tokenpak cards` prints help.
    p_cards.set_defaults(func=lambda a: p_cards.print_help())


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _err(msg: str) -> int:
    print(f"✗ tokenpak cards — {msg}", file=sys.stderr)
    return 1


def _reject_worker_type(args: Any) -> Optional[int]:
    if getattr(args, "card_type", None) == "worker":
        return _err(
            "--type worker is Phase 2 — WorkerProfile has no canonical contract yet (Std 54 §B)"
        )
    return None


def _project_root() -> Path:
    return Path.cwd()


def _select_cards(args: Any, root: Path) -> list[Path]:
    """Resolve the card files an action operates on (explicit PATH or all)."""
    from tokenpak.cards.discover import discover_cards
    from tokenpak.cards.model import CardError

    path = getattr(args, "path", None)
    if path:
        p = Path(path)
        if not p.is_file():
            raise CardError(f"card file not found: {p}")
        return [p]
    return discover_cards(root, card_type=getattr(args, "card_type", None))


def _resolve_by_name(name: str, args: Any, root: Path):
    """Find a parsed card by frontmatter name (mode-aware, Std 54 §G)."""
    from tokenpak.cards.discover import discover_cards, load_installed
    from tokenpak.cards.model import MODE_LOCKED, CardError
    from tokenpak.cards.parser import parse_card_file

    mode = getattr(args, "mode", "dev")
    candidates: list[Path] = []
    if mode == MODE_LOCKED:
        store = load_installed(root)
        entry = store["cards"].get(name)
        if entry is None:
            raise CardError(
                f"card {name!r} is not installed (locked mode loads "
                "installed cards only — Std 54 §G)"
            )
        candidates = [Path(entry["source_path"])]
    else:
        candidates = discover_cards(root, card_type=getattr(args, "card_type", None))

    for path in candidates:
        try:
            card = parse_card_file(path)
        except CardError:
            continue
        if card.name == name:
            return card
    raise CardError(
        f"no card named {name!r} found in {root}. "
        "Run `tokenpak cards list` to see available card names."
    )


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def cmd_cards_discover(args: Any) -> int:
    rc = _reject_worker_type(args)
    if rc is not None:
        return rc
    from tokenpak.cards.discover import (
        card_kind_for_path,
        discover_cards,
        has_project_index,
    )
    from tokenpak.cards.model import MODE_LOCKED

    root = _project_root()
    if args.mode == MODE_LOCKED:
        return _err(
            "locked mode disables project-tree discovery (Std 54 §G) — "
            "use `tokenpak cards list --mode locked` for installed cards"
        )

    paths = discover_cards(root, card_type=args.card_type)
    index_present = has_project_index(root)
    if getattr(args, "json", False):
        print(
            json.dumps(
                {
                    "project_root": str(root),
                    "project_index_present": index_present,
                    "cards": [{"path": str(p), "card_kind": card_kind_for_path(p)} for p in paths],
                },
                indent=2,
            )
        )
        return 0

    print(f"Cards discovered under {root}")
    print("─" * 40)
    if not paths:
        print("  (none — expected layout: integrations/*.tip.md, paks/*.pak.md)")
    for p in paths:
        kind = card_kind_for_path(p) or "?"
        print(f"  [{kind}] {p.relative_to(root)}")
    if not index_present:
        print(
            "⚠️  No .tokenpak.md project index — cards are not loadable in "
            "dev discovery until it exists (Std 54 §G)."
        )
    return 0


def cmd_cards_validate(args: Any) -> int:
    rc = _reject_worker_type(args)
    if rc is not None:
        return rc
    from tokenpak.cards.model import CardError
    from tokenpak.cards.parser import parse_card_file
    from tokenpak.cards.validate import validate_card

    root = _project_root()
    try:
        paths = _select_cards(args, root)
    except CardError as exc:
        return _err(str(exc))
    if not paths:
        return _err(
            "no cards found to validate. Run `tokenpak cards scaffold "
            "--type tip --name <name>` to create one, or "
            "`tokenpak cards discover` to see what's present."
        )

    reports = []
    failed = 0
    for path in paths:
        try:
            card = parse_card_file(path)
        except CardError as exc:
            failed += 1
            reports.append({"path": str(path), "ok": False, "errors": [str(exc)], "warnings": []})
            continue
        report = validate_card(card, mode=args.mode, strict=args.strict)
        if not report.ok:
            failed += 1
        reports.append(report.to_dict())

    if getattr(args, "json", False):
        print(json.dumps({"ok": failed == 0, "cards": reports}, indent=2))
        return 0 if failed == 0 else 1

    for r in reports:
        icon = "✅" if r["ok"] else "❌"
        print(f"{icon} {r['path']}")
        for e in r["errors"]:
            print(f"     ✗ {e}")
        for w in r["warnings"]:
            print(f"     ⚠ {w}")
    print()
    print(f"{len(reports) - failed} valid / {failed} invalid")
    return 0 if failed == 0 else 1


def cmd_cards_compile(args: Any) -> int:
    rc = _reject_worker_type(args)
    if rc is not None:
        return rc
    from tokenpak.cards.compile import compile_card, write_compiled
    from tokenpak.cards.discover import compiled_dir
    from tokenpak.cards.model import CardError
    from tokenpak.cards.parser import parse_card_file

    root = _project_root()
    try:
        paths = _select_cards(args, root)
    except CardError as exc:
        return _err(str(exc))
    if not paths:
        return _err(
            "no cards found to compile. Run `tokenpak cards scaffold "
            "--type tip --name <name>` to create one, or "
            "`tokenpak cards discover` to see what's present."
        )

    out_dir = compiled_dir(root)
    written: list[dict[str, str]] = []
    for path in paths:
        try:
            card = parse_card_file(path)
            manifest = compile_card(card, mode=args.mode, strict=args.strict)
            out = write_compiled(manifest, out_dir)
        except CardError as exc:
            return _err(f"{path}: {exc}")
        written.append({"source": str(path), "manifest": str(out)})

    if getattr(args, "json", False):
        print(json.dumps({"compiled": written}, indent=2))
        return 0
    for w in written:
        print(f"✅ Compiled {w['source']} → {w['manifest']}")
    return 0


def cmd_cards_install(args: Any) -> int:
    rc = _reject_worker_type(args)
    if rc is not None:
        return rc
    from tokenpak.cards.compile import compile_card, write_compiled
    from tokenpak.cards.discover import (
        compiled_dir,
        installed_manifest_path,
        record_install,
    )
    from tokenpak.cards.model import CardError
    from tokenpak.cards.parser import parse_card_file

    root = _project_root()
    try:
        paths = _select_cards(args, root)
    except CardError as exc:
        return _err(str(exc))
    if not paths:
        return _err(
            "no cards found to install. Run `tokenpak cards scaffold "
            "--type tip --name <name>` to create one, or "
            "`tokenpak cards discover` to see what's present."
        )

    out_dir = compiled_dir(root)
    installed: list[dict[str, str]] = []
    for path in paths:
        try:
            card = parse_card_file(path)
            manifest = compile_card(card, mode=args.mode, strict=args.strict)
            out = write_compiled(manifest, out_dir)
            record_install(
                root,
                name=str(manifest["name"]),
                kind=str(manifest["card_kind"]),
                source_path=path,
                source_sha256=card.source_sha256,
                compiled_path=out,
            )
        except CardError as exc:
            return _err(f"{path}: {exc}")
        installed.append(
            {
                "name": str(manifest["name"]),
                "kind": str(manifest["card_kind"]),
                "manifest": str(out),
            }
        )

    if getattr(args, "json", False):
        print(
            json.dumps(
                {
                    "installed": installed,
                    "manifest": str(installed_manifest_path(root)),
                },
                indent=2,
            )
        )
        return 0
    for item in installed:
        print(f"✅ Installed {item['name']} ({item['kind']}) → {item['manifest']}")
    print(f"   recorded in {installed_manifest_path(root)}")
    return 0


def cmd_cards_list(args: Any) -> int:
    rc = _reject_worker_type(args)
    if rc is not None:
        return rc
    from tokenpak.cards.discover import (
        card_kind_for_path,
        discover_cards,
        load_installed,
        user_global_installed,
    )
    from tokenpak.cards.model import MODE_LOCKED, CardError

    root = _project_root()
    try:
        store = load_installed(root)
    except CardError as exc:
        return _err(str(exc))
    installed = store["cards"]

    discovered: list[Path] = []
    if args.mode != MODE_LOCKED:
        discovered = discover_cards(root, card_type=args.card_type)

    global_cards = user_global_installed()

    if getattr(args, "json", False):
        print(
            json.dumps(
                {
                    "mode": args.mode,
                    "discovered": [
                        {"path": str(p), "card_kind": card_kind_for_path(p)} for p in discovered
                    ],
                    "installed": installed,
                    "user_global_installed": sorted(global_cards),
                },
                indent=2,
            )
        )
        return 0

    print(f"Cards ({args.mode} mode)")
    print("─" * 40)
    if args.mode == MODE_LOCKED:
        print("  (locked: installed cards only — no project-tree discovery)")
    else:
        print(f"  Discovered ({len(discovered)}):")
        for p in discovered:
            kind = card_kind_for_path(p) or "?"
            mark = (
                " [installed]"
                if any(e.get("source_path") == str(p) for e in installed.values())
                else ""
            )
            print(f"    [{kind}] {p.relative_to(root)}{mark}")
    print(f"  Installed ({len(installed)}):")
    for name, entry in sorted(installed.items()):
        print(f"    [{entry.get('card_kind', '?')}] {name} ← {entry.get('source_path')}")
    if global_cards:
        print(f"  User-global installed ({len(global_cards)}): {', '.join(sorted(global_cards))}")
    return 0


def cmd_cards_inspect(args: Any) -> int:
    rc = _reject_worker_type(args)
    if rc is not None:
        return rc
    from tokenpak.cards.model import CardError
    from tokenpak.cards.parser import scan_env_references
    from tokenpak.cards.validate import validate_card

    root = _project_root()
    try:
        card = _resolve_by_name(args.name, args, root)
    except CardError as exc:
        return _err(str(exc))

    report = validate_card(card, mode=args.mode)
    fm = card.frontmatter
    payload = {
        "name": card.name,
        "path": card.path,
        "card_kind": card.card_kind,
        "tip_kind": card.tip_kind,
        "target_contract": fm.get("target_contract"),
        "card_status": fm.get("card_status"),
        "capabilities": sorted(card.capabilities),
        "category_tags": fm.get("category_tags") or [],
        "advisory_risk_flags": fm.get("advisory_risk_flags") or [],
        "env_references": scan_env_references(card),
        "valid": report.ok,
        "warnings": [i.message for i in report.warnings],
        "errors": [i.message for i in report.errors],
    }
    if card.card_kind == "pak":
        payload["pak_fields"] = {
            k: fm.get(k)
            for k in (
                "pak_subtype",
                "pak_status",
                "authority",
                "confidence",
                "retention",
                "privacy",
            )
        }

    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2))
        return 0

    print(f"Card {payload['name']} ({payload['card_kind']})")
    print("─" * 40)
    print(f"  path            : {payload['path']}")
    if payload["tip_kind"]:
        print(f"  tip_kind        : {payload['tip_kind']}")
    print(f"  target_contract : {payload['target_contract']}")
    print(f"  card_status     : {payload['card_status']}")
    badge = "✅ valid" if payload["valid"] else "❌ invalid"
    print(f"  validation      : {badge}")
    if payload["capabilities"]:
        print(f"  capabilities    : {', '.join(payload['capabilities'])}")
    if payload.get("pak_fields"):
        for k, v in payload["pak_fields"].items():
            print(f"  {k:<15} : {v}")
    if payload["category_tags"]:
        print(f"  category_tags   : {', '.join(payload['category_tags'])}")
    if payload["advisory_risk_flags"]:
        print(
            f"  advisory_risk_flags (no runtime effect): "
            f"{', '.join(payload['advisory_risk_flags'])}"
        )
    if payload["env_references"]:
        print(f"  env references  : {', '.join(payload['env_references'])}")
    for e in payload["errors"]:
        print(f"  ✗ {e}")
    for w in payload["warnings"]:
        print(f"  ⚠ {w}")
    return 0 if payload["valid"] else 1


def cmd_cards_preview(args: Any) -> int:
    from tokenpak.cards.model import CardError
    from tokenpak.cards.validate import validate_card

    root = _project_root()
    try:
        card = _resolve_by_name(args.name, args, root)
    except CardError as exc:
        return _err(str(exc))

    report = validate_card(card, mode=args.mode)
    if not report.ok:
        return _err(
            f"card {args.name!r} fails validation — fix it before preview: "
            + "; ".join(i.message for i in report.errors)
        )

    fm = card.frontmatter
    declares_recall = "tip.pak.recall" in card.capabilities

    pro_state = None
    if args.pro:
        try:
            from tokenpak.licensing.daemon_probe import detect_daemon_state

            pro_state = detect_daemon_state()
        except Exception:
            pro_state = "unavailable"

    payload = {
        "name": card.name,
        "card_kind": card.card_kind,
        "preview_kind": "static-declared-scope",
        "ranking": "none — unranked; install Pro Local for scoring",
        "declared_capabilities": sorted(card.capabilities),
        "declared_scope": {
            k: fm.get(k)
            for k in ("pak_subtype", "category_tags", "advisory_risk_flags")
            if fm.get(k) is not None
        },
        "live_candidates": None,
        "notes": [],
    }
    if not declares_recall:
        payload["notes"].append(
            "card does not declare tip.pak.recall — live candidate preview "
            "is unavailable (Std 54 §I)"
        )
    else:
        payload["notes"].append(
            "live unranked candidates require a registered connector "
            "(Phase 2) — showing static declarations only"
        )
    if args.pro:
        payload["pro_daemon_state"] = pro_state
        if pro_state != "active":
            payload["notes"].append(
                "Pro Local daemon not available — falling back to OSS "
                "unranked static preview (Std 54 §I)"
            )
        else:
            payload["notes"].append(
                "scored preview is served by the Pro Local surface "
                "(tokenpak-paid); the OSS CLI shows the static dump"
            )

    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2))
        return 0

    print(f"Preview — {payload['name']} ({payload['card_kind']})")
    print("─" * 40)
    print("  unranked — install Pro Local for scoring.")
    if payload["declared_capabilities"]:
        print(f"  capabilities : {', '.join(payload['declared_capabilities'])}")
    for k, v in payload["declared_scope"].items():
        print(f"  {k:<12} : {v}")
    for note in payload["notes"]:
        print(f"  note: {note}")
    return 0


def cmd_cards_scaffold(args: Any) -> int:
    from tokenpak.cards.model import CardError
    from tokenpak.cards.scaffold import scaffold_card

    root = _project_root()
    try:
        created = scaffold_card(
            card_type=args.card_type,
            name=args.name,
            kind=args.kind,
            project_root=root,
        )
    except CardError as exc:
        return _err(str(exc))
    if getattr(args, "json", False):
        print(
            json.dumps(
                {"scaffolded": [str(p.relative_to(root)) for p in created]},
                indent=2,
            )
        )
        return 0
    print(f"✅ Scaffolded {args.card_type} card {args.name!r}:")
    for p in created:
        print(f"   {p.relative_to(root)}")
    print("   Next: edit the card, then `tokenpak cards validate` + `compile`.")
    return 0


def cmd_cards_doctor(args: Any) -> int:
    from tokenpak.cards.discover import (
        compiled_dir,
        discover_cards,
        has_project_index,
        load_installed,
        user_global_cards_dir,
    )
    from tokenpak.cards.model import CardError
    from tokenpak.cards.parser import parse_card_file
    from tokenpak.cards.validate import validate_card

    root = _project_root()
    checks: list[tuple[str, str, str]] = []  # (status, name, detail)

    index_ok = has_project_index(root)
    checks.append(
        (
            "PASS" if index_ok else "WARN",
            "project-index",
            ".tokenpak.md present"
            if index_ok
            else ".tokenpak.md missing — dev discovery will not load cards (Std 54 §G)",
        )
    )

    paths = discover_cards(root)
    n_err = 0
    n_warn = 0
    for path in paths:
        try:
            card = parse_card_file(path)
        except CardError as exc:
            n_err += 1
            checks.append(("FAIL", "parse", f"{path}: {exc}"))
            continue
        report = validate_card(card, mode=args.mode)
        n_err += len(report.errors)
        n_warn += len(report.warnings)
        for issue in report.issues:
            status = "FAIL" if issue.severity == "error" else "WARN"
            checks.append((status, "validate", f"{path}: {issue.message}"))
    checks.append(
        (
            "PASS" if n_err == 0 else "FAIL",
            "cards",
            f"{len(paths)} card(s) discovered, {n_err} error(s), {n_warn} warning(s)",
        )
    )

    try:
        store = load_installed(root)
        stale = [
            name
            for name, entry in store["cards"].items()
            if not Path(entry.get("source_path", "")).is_file()
        ]
        if stale:
            checks.append(("WARN", "installed", f"installed cards with missing sources: {stale}"))
        else:
            checks.append(("PASS", "installed", f"{len(store['cards'])} card(s) installed"))
    except CardError as exc:
        checks.append(("FAIL", "installed", str(exc)))

    cdir = compiled_dir(root)
    n_compiled = len(list(cdir.glob("*.json"))) if cdir.is_dir() else 0
    checks.append(("PASS", "compiled-cache", f"{n_compiled} manifest(s) in {cdir}"))

    gdir = user_global_cards_dir()
    if gdir is not None:
        checks.append(("PASS", "user-global", f"user-global cards home resolves to {gdir}"))

    has_fail = any(s == "FAIL" for s, _, _ in checks)
    if getattr(args, "json", False):
        print(
            json.dumps(
                {
                    "ok": not has_fail,
                    "checks": [{"status": s, "name": n, "detail": d} for s, n, d in checks],
                },
                indent=2,
            )
        )
        return 1 if has_fail else 0

    icons = {"PASS": "✅", "WARN": "⚠️", "FAIL": "❌"}
    print("Cards authoring-layer diagnostics")
    print("─" * 40)
    for status, name, detail in checks:
        print(f"{icons[status]} {name:<16} {detail}")
    return 1 if has_fail else 0


__all__ = [
    "build_cards_parser",
    "cmd_cards_compile",
    "cmd_cards_discover",
    "cmd_cards_doctor",
    "cmd_cards_inspect",
    "cmd_cards_install",
    "cmd_cards_list",
    "cmd_cards_preview",
    "cmd_cards_scaffold",
    "cmd_cards_validate",
]
