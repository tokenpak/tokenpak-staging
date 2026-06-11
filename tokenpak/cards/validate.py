# SPDX-License-Identifier: Apache-2.0
"""Card validation (Std 54 §C/§E/§H).

Validation classifies findings as errors (block compile/install) or
warnings (allowed in dev discovery, Std 54 §G). The card→adapter
consistency check (§H) reads the adapter module **statically via
``ast.parse``** — adapter code is never imported or executed by the
authoring layer.
"""

from __future__ import annotations

import ast
import re
import warnings as _warnings
from pathlib import Path
from typing import Any, Optional

from tokenpak.cards.model import (
    CARD_STATUSES,
    MODE_DEV,
    MODE_LOCKED,
    PHASE1_CARD_KINDS,
    PHASE1_TIP_KINDS,
    PHASE2_CARD_KINDS,
    PHASE2_TIP_KINDS,
    SOURCE_FORMAT,
    TARGET_CONTRACT_PAK,
    TARGET_CONTRACT_PROVIDER_ADAPTER,
    CardValidationReport,
    ParsedCard,
)

# Required frontmatter keys common to both Phase-1 card kinds (Std 54 §C).
_REQUIRED_COMMON_KEYS = (
    "card_kind",
    "name",
    "target_contract",
    "source_format",
    "card_status",
)

# Pak-specific canonical enum fields (Std 54 §C / §D). ``retention`` is
# resolved via the contract's own default-retention discovery when absent.
_REQUIRED_PAK_ENUM_KEYS = ("pak_subtype", "pak_status", "authority", "confidence", "privacy")

# Keys this validator understands; anything else is a warning (cards are
# forgiving authoring inputs — Std 54 §0).
_KNOWN_KEYS = frozenset(
    {
        *_REQUIRED_COMMON_KEYS,
        *_REQUIRED_PAK_ENUM_KEYS,
        "tip_kind",
        "tokenpak_min_version",
        "retention",
        "category_tags",
        "advisory_risk_flags",
        "capabilities",
    }
)

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_CAPABILITY_RE = re.compile(r"^(tip|ext)\.[a-z0-9._-]+$")
_VERSION_RE = re.compile(r"^\d+(\.\d+){0,2}([a-zA-Z0-9.\-+]*)?$")


# ---------------------------------------------------------------------------
# Main validator
# ---------------------------------------------------------------------------


def validate_card(
    card: ParsedCard,
    *,
    mode: str = MODE_DEV,
    strict: bool = False,
) -> CardValidationReport:
    """Validate a parsed card against the Std 54 Phase-1 contract.

    ``mode``/``strict`` only change the §H consistency relation
    (subset-by-default in dev; equality in locked/strict). Overclaiming
    is always an error regardless of mode.
    """
    report = CardValidationReport(path=card.path, name=card.name)
    fm = card.frontmatter

    # -- required common keys ----------------------------------------------
    for key in _REQUIRED_COMMON_KEYS:
        if key not in fm:
            report.error("missing-field", f"required frontmatter field missing: {key}")
    if report.errors:
        # Without the common keys the kind-specific checks would cascade.
        return report

    kind = fm.get("card_kind")
    name = fm.get("name")
    status = fm.get("card_status")
    source_format = fm.get("source_format")

    # -- card_kind ----------------------------------------------------------
    if kind in PHASE2_CARD_KINDS:
        report.error(
            "phase2-kind",
            f"card_kind {kind!r} is Phase 2 — its canonical contract is not "
            "ratified yet (Std 54 §B). Phase-1 kinds: tip, pak.",
        )
        return report
    if kind == "project":
        report.error(
            "not-a-card",
            "'project' is not a card_kind — .tokenpak.md is a project index "
            "outside the Card system (Std 54 §B).",
        )
        return report
    if kind not in PHASE1_CARD_KINDS:
        report.error(
            "unknown-kind",
            f"unknown card_kind {kind!r} (Phase-1 kinds: tip, pak)",
        )
        return report

    # -- name ----------------------------------------------------------------
    if not isinstance(name, str) or not name:
        report.error("bad-name", "name must be a non-empty string")
    elif not _NAME_RE.match(name):
        report.error(
            "bad-name",
            f"name {name!r} must be a lowercase slug "
            "(letters/digits/dot/dash/underscore, no path separators)",
        )

    # -- source_format / card_status -----------------------------------------
    if source_format != SOURCE_FORMAT:
        report.error(
            "bad-source-format",
            f"source_format must be {SOURCE_FORMAT!r}, got {source_format!r}",
        )
    if status not in CARD_STATUSES:
        report.error(
            "bad-card-status",
            f"card_status must be one of {list(CARD_STATUSES)}, got {status!r}",
        )

    # -- tokenpak_min_version --------------------------------------------------
    min_ver = fm.get("tokenpak_min_version")
    if min_ver is None:
        report.warning(
            "missing-min-version",
            "tokenpak_min_version not declared (recommended per Std 54 §C)",
        )
    elif not isinstance(min_ver, str) or not _VERSION_RE.match(min_ver):
        report.error(
            "bad-min-version",
            f"tokenpak_min_version must be a version string, got {min_ver!r}",
        )

    # -- kind-specific -----------------------------------------------------------
    if kind == "tip":
        _validate_tip(card, report)
    else:
        _validate_pak(card, report)

    # -- capabilities (both kinds may declare them; Std 54 §D/§E) ---------------
    _validate_capabilities(fm.get("capabilities"), report)

    # -- card-only authoring metadata --------------------------------------------
    for meta_key in ("category_tags", "advisory_risk_flags"):
        v = fm.get(meta_key)
        if v is not None and (
            not isinstance(v, list) or any(not isinstance(s, str) for s in v)
        ):
            report.error("bad-metadata", f"{meta_key} must be a list of strings")
    if "risk_flags" in fm:
        report.warning(
            "renamed-field",
            "'risk_flags' has no canonical Pak field and no runtime "
            "enforcement — use 'advisory_risk_flags' (Std 54 §C rename rationale)",
        )

    # -- unknown keys (forgiving authoring input) ----------------------------------
    for key in fm:
        if key not in _KNOWN_KEYS and key != "risk_flags":
            report.warning("unknown-field", f"unknown frontmatter field: {key}")

    # -- §H consistency (tip provider_adapter only) ----------------------------------
    if kind == "tip" and report.ok:
        check_consistency(card, report, mode=mode, strict=strict)

    return report


# ---------------------------------------------------------------------------
# Kind-specific checks
# ---------------------------------------------------------------------------


def _validate_tip(card: ParsedCard, report: CardValidationReport) -> None:
    fm = card.frontmatter
    tip_kind = fm.get("tip_kind")
    if tip_kind in PHASE2_TIP_KINDS:
        report.error(
            "phase2-kind",
            f"tip_kind {tip_kind!r} is Phase 2 — requires a new canonical "
            "contract that is not ratified yet (Std 54 §B).",
        )
        return
    if tip_kind not in PHASE1_TIP_KINDS:
        report.error(
            "bad-tip-kind",
            f"tip_kind must be 'provider_adapter' in Phase 1, got {tip_kind!r}",
        )
        return
    target = fm.get("target_contract")
    if target != TARGET_CONTRACT_PROVIDER_ADAPTER:
        report.error(
            "bad-target-contract",
            f"provider_adapter cards must declare target_contract "
            f"{TARGET_CONTRACT_PROVIDER_ADAPTER!r}, got {target!r}",
        )


def _validate_pak(card: ParsedCard, report: CardValidationReport) -> None:
    from tokenpak.tip.pak import (
        PakAuthority,
        PakConfidence,
        PakPrivacyClass,
        PakRetention,
        PakStatus,
        PakSubtype,
    )

    fm = card.frontmatter
    target = fm.get("target_contract")
    if target != TARGET_CONTRACT_PAK:
        report.error(
            "bad-target-contract",
            f"pak cards must declare target_contract {TARGET_CONTRACT_PAK!r}, "
            f"got {target!r}",
        )

    for key in _REQUIRED_PAK_ENUM_KEYS:
        if key not in fm:
            report.error("missing-field", f"required pak frontmatter field missing: {key}")
    if report.errors:
        return

    # pak_subtype — canonical 5-value taxonomy; deprecated aliases resolve
    # with a warning (Std 54 §A invariant 6; pak.py legacy alias table).
    subtype_raw = fm.get("pak_subtype")
    if not isinstance(subtype_raw, str):
        report.error("bad-enum", f"pak_subtype must be a string, got {subtype_raw!r}")
    else:
        with _warnings.catch_warnings(record=True) as caught:
            _warnings.simplefilter("always")
            try:
                PakSubtype.parse(subtype_raw)
            except ValueError:
                report.error(
                    "bad-enum",
                    f"pak_subtype {subtype_raw!r} is not canonical "
                    f"(allowed: {[s.value for s in PakSubtype]})",
                )
            else:
                for w in caught:
                    if issubclass(w.category, DeprecationWarning):
                        report.warning("deprecated-alias", str(w.message))

    _check_enum(fm, "pak_status", PakStatus, report)
    _check_enum(fm, "authority", PakAuthority, report)
    _check_enum(fm, "confidence", PakConfidence, report)
    _check_enum(fm, "privacy", PakPrivacyClass, report)
    if "retention" in fm:
        _check_enum(fm, "retention", PakRetention, report)


def _check_enum(fm: Any, key: str, enum_cls: Any, report: CardValidationReport) -> None:
    value = fm.get(key)
    try:
        enum_cls(value)
    except ValueError:
        report.error(
            "bad-enum",
            f"{key} {value!r} is not canonical "
            f"(allowed: {[e.value for e in enum_cls]})",
        )


def _validate_capabilities(value: Any, report: CardValidationReport) -> None:
    """Capability declarations come from capabilities.py only (Std 54 §E).

    ``tip.*`` strings must exist in the canonical vocabulary — inventing
    capability strings inline is blocked (invariant 8 / Std 31 §2).
    ``ext.<vendor>.<feature>`` strings are metadata/hints only.
    """
    if value is None:
        return
    if not isinstance(value, list) or any(not isinstance(s, str) for s in value):
        report.error("bad-capabilities", "capabilities must be a list of strings")
        return
    from tokenpak.tip.capabilities import ALL_OPTIMIZATION_CAPABILITIES

    if len(set(value)) != len(value):
        report.error("bad-capabilities", "capabilities must be unique")
    for cap in value:
        if not _CAPABILITY_RE.match(cap):
            report.error(
                "bad-capabilities",
                f"capability {cap!r} does not match "
                "tip.<group>.<feature> / ext.<vendor>.<feature>",
            )
        elif cap.startswith("tip.") and cap not in ALL_OPTIMIZATION_CAPABILITIES:
            report.error(
                "invented-capability",
                f"capability {cap!r} is not in the canonical vocabulary "
                "(tokenpak/tip/capabilities.py) — cards MUST NOT invent "
                "capability strings inline (Std 54 §E / Std 31 §2)",
            )
        elif cap.startswith("ext."):
            report.warning(
                "ext-capability",
                f"{cap!r} is a vendor extension — metadata/hint only; the "
                "core runtime makes no policy decisions on ext.* (Std 54 §E)",
            )


# ---------------------------------------------------------------------------
# §H — card-to-adapter consistency
# ---------------------------------------------------------------------------


def find_adapter_module(card: ParsedCard) -> Optional[Path]:
    """Locate the adapter module for a provider_adapter card.

    Convention (Std 54 §J scaffold layout): ``adapter.py`` next to the
    card source. Returns None when the card has no on-disk path or no
    sibling adapter module.
    """
    if not card.path:
        return None
    candidate = Path(card.path).parent / "adapter.py"
    return candidate if candidate.is_file() else None


def extract_adapter_capabilities(adapter_path: Path) -> Optional[frozenset[str]]:
    """Statically extract the adapter's declared capability frozenset.

    Reads ``adapter.py`` via ``ast.parse`` — the module is **never
    imported or executed**. Recognizes module- or class-level
    assignments to ``capabilities`` / ``CAPABILITIES`` of the forms
    ``frozenset({...})``, ``frozenset((...))``, ``frozenset([...])`` or
    a plain set literal of string constants. Multiple declarations are
    unioned. Returns None when no declaration is found.
    """
    try:
        tree = ast.parse(adapter_path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return None

    found: set[str] = set()
    seen_any = False
    for node in ast.walk(tree):
        targets: list[ast.expr] = []
        value: Optional[ast.expr] = None
        if isinstance(node, ast.Assign):
            targets = node.targets
            value = node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
            value = node.value
        else:
            continue
        names = {t.id for t in targets if isinstance(t, ast.Name)}
        if not names & {"capabilities", "CAPABILITIES"}:
            continue
        caps = _literal_string_set(value)
        if caps is not None:
            seen_any = True
            found |= caps
    return frozenset(found) if seen_any else None


def _literal_string_set(node: Optional[ast.expr]) -> Optional[set[str]]:
    if node is None:
        return None
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "frozenset"
    ):
        if not node.args:
            return set()
        return _literal_string_set(node.args[0])
    if isinstance(node, (ast.Set, ast.List, ast.Tuple)):
        out: set[str] = set()
        for elt in node.elts:
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                out.add(elt.value)
            else:
                return None
        return out
    return None


def check_consistency(
    card: ParsedCard,
    report: CardValidationReport,
    *,
    mode: str = MODE_DEV,
    strict: bool = False,
) -> None:
    """Std 54 §H card-to-adapter consistency check.

    * Default (dev): ``card ⊆ adapter`` required; an adapter supporting
      more than the card declares is a warning, not an error.
    * Strict (``--mode locked`` or ``--strict``): exact equality.
    * Always blocked: the card declaring a capability NOT in the
      adapter's frozenset (overclaiming).
    """
    exact = strict or mode == MODE_LOCKED
    card_caps = frozenset(card.capabilities)

    adapter_path = find_adapter_module(card)
    if adapter_path is None:
        if exact:
            report.error(
                "adapter-missing",
                "strict consistency requires an adapter module "
                "(adapter.py next to the card) — none found (Std 54 §H)",
            )
        else:
            report.warning(
                "adapter-missing",
                "no adapter.py found next to the card — card→adapter "
                "consistency not checked (declaration-only card)",
            )
        return

    adapter_caps = extract_adapter_capabilities(adapter_path)
    if adapter_caps is None:
        msg = (
            f"could not statically read a capabilities frozenset from "
            f"{adapter_path.name} (expected `capabilities = frozenset({{...}})`)"
        )
        if exact:
            report.error("adapter-unreadable", msg)
        else:
            report.warning("adapter-unreadable", msg)
        return

    overclaimed = card_caps - adapter_caps
    if overclaimed:
        report.error(
            "overclaimed-capability",
            "card declares capabilities not in the adapter's frozenset "
            f"(always blocked, Std 54 §H): {sorted(overclaimed)}",
        )
        return

    underclaimed = adapter_caps - card_caps
    if underclaimed:
        if exact:
            report.error(
                "capability-mismatch",
                "strict mode requires card == adapter capabilities; adapter "
                f"declares extra: {sorted(underclaimed)} (Std 54 §H)",
            )
        else:
            report.warning(
                "capability-subset",
                "adapter supports more than the card declares: "
                f"{sorted(underclaimed)} (allowed in dev mode, Std 54 §H)",
            )


__all__ = [
    "check_consistency",
    "extract_adapter_capabilities",
    "find_adapter_module",
    "validate_card",
]
