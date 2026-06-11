# SPDX-License-Identifier: Apache-2.0
"""Card compiler — validated card → canonical JSON manifest (Std 54 §C/§D).

The compiled manifest is the ONLY artifact the runtime may trust
(invariants 1–4): cards are source files; raw Markdown is never
executed; every active card compiles to a canonical ``target_contract``.

Compile targets (Phase 1):

* ``.pak.md`` → :class:`tokenpak.tip.pak.Pak` wire dict. The compiler
  proves canonicality by round-tripping the dict through
  ``Pak.from_dict(...)`` / ``to_dict()`` — if the frozen dataclass
  rejects it, compilation fails.
* ``.tip.md tip_kind: provider_adapter`` → provider adapter manifest
  with the spec ``{Name}CredentialProvider`` class-name
  derivation and the declared capability set.

Schema stamping (Std 54 §C): pak manifests stamp ``pak-v1.json``
(registry mirror of the contract versioned in ``pak.py``);
capability-bearing manifests additionally stamp
``tip-capabilities.v1.json``. Not every compiled card stamps the
capabilities schema — only capability-bearing ones.
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Any, Optional

from tokenpak.cards.model import (
    CAPABILITIES_SCHEMA,
    MANIFEST_VERSION,
    MODE_DEV,
    PAK_TARGET_SCHEMA,
    SOURCE_FORMAT,
    CardError,
    ParsedCard,
)
from tokenpak.cards.validate import (
    extract_adapter_capabilities,
    find_adapter_module,
    validate_card,
)


def _utc_now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def compile_card(
    card: ParsedCard,
    *,
    mode: str = MODE_DEV,
    strict: bool = False,
) -> dict[str, Any]:
    """Compile a card to its canonical JSON manifest.

    Validates first; any validation error raises :class:`CardError`
    (no manifest is produced from an invalid card — invariant 4).
    """
    report = validate_card(card, mode=mode, strict=strict)
    if not report.ok:
        details = "; ".join(i.message for i in report.errors)
        raise CardError(f"card failed validation: {details}")

    fm = card.frontmatter
    manifest: dict[str, Any] = {
        "manifest_version": MANIFEST_VERSION,
        "source_format": SOURCE_FORMAT,
        "card_kind": fm["card_kind"],
        "name": fm["name"],
        "target_contract": fm["target_contract"],
        "card_status": fm["card_status"],
        "tokenpak_min_version": fm.get("tokenpak_min_version"),
        "source_path": card.path,
        "source_sha256": card.source_sha256,
        "compiled_at": _utc_now_iso(),
        "warnings": [i.message for i in report.warnings],
    }

    caps = sorted(card.capabilities)
    if caps:
        # Only capability-bearing cards stamp the capabilities schema
        # (Std 54 §C).
        manifest["capabilities"] = caps
        manifest["capabilities_schema"] = CAPABILITIES_SCHEMA

    # Card-only authoring metadata — carried in the manifest envelope,
    # NOT inside the canonical contract payload (no canonical Pak field
    # exists; no runtime effect today — Std 54 §D).
    authoring_meta: dict[str, Any] = {}
    for key in ("category_tags", "advisory_risk_flags"):
        if isinstance(fm.get(key), list):
            authoring_meta[key] = list(fm[key])
    if authoring_meta:
        manifest["authoring_metadata"] = authoring_meta

    if fm["card_kind"] == "pak":
        manifest["target_schema"] = PAK_TARGET_SCHEMA
        manifest["pak"] = _compile_pak_payload(card)
    else:
        manifest["adapter"] = _compile_adapter_payload(card)

    return manifest


# ---------------------------------------------------------------------------
# .pak.md → tokenpak.tip.Pak
# ---------------------------------------------------------------------------


def _compile_pak_payload(card: ParsedCard) -> dict[str, Any]:
    """Build the canonical Pak wire dict and prove it via round-trip.

    Field derivation notes (the Std 54 draft defines no card-body syntax
    for the body-borne §D fields — see the build flags):

    * ``pak_id``: content-addressed from the card source bytes
      (mirrors the ``pak create`` checksum-id convention).
    * ``title``: first H1 in the body, else the card ``name``.
    * ``summary``: the body text (provisional compiler default).
    * ``scope`` / ``anchors`` / ``relationships``: contract defaults —
      the §D "(body)" syntax for these is not defined in the Phase-1
      draft, so the compiler does not read them from the body.
    * ``source``: compile-time provenance of the card file itself.
    * ``retention``: frontmatter value, else the contract's own
      ``default_retention_for(subtype)`` discovery.
    """
    import warnings as _warnings

    from tokenpak.tip.pak import Pak, PakSubtype, default_retention_for

    fm = card.frontmatter
    with _warnings.catch_warnings():
        _warnings.simplefilter("ignore", DeprecationWarning)
        subtype = PakSubtype.parse(str(fm["pak_subtype"]))

    retention = fm.get("retention") or default_retention_for(subtype).value
    title = card.body_title() or str(fm["name"])
    summary = _body_summary(card)

    wire: dict[str, Any] = {
        "pak_id": f"pak:{card.source_sha256[:16]}",
        "pak_type": subtype.value,
        "title": title,
        "summary": summary,
        "scope": {},
        "source": {
            "platform": "tokenpak-cards",
            "source_type": "file",
            "created_at": _utc_now_iso(),
            "source_hash": card.source_sha256,
        },
        "status": fm["pak_status"],
        "authority": fm["authority"],
        "confidence": fm["confidence"],
        "retention": {"ttl": retention},
        "privacy": {"class": fm["privacy"]},
        "anchors": [],
        "relationships": {},
    }

    # Canonicality proof: the frozen dataclass is the authority. A wire
    # dict the contract cannot parse must never reach the runtime.
    try:
        pak = Pak.from_dict(wire)
    except (KeyError, ValueError, TypeError) as exc:
        raise CardError(f"compiled Pak payload rejected by contract: {exc}") from exc
    return pak.to_dict()


def _body_summary(card: ParsedCard) -> str:
    """Body text with the title H1 removed — provisional summary source."""
    lines = []
    skipped_h1 = False
    for line in card.body.splitlines():
        if not skipped_h1 and line.strip().startswith("# "):
            skipped_h1 = True
            continue
        lines.append(line)
    return "\n".join(lines).strip()


# ---------------------------------------------------------------------------
# .tip.md provider_adapter → adapter manifest
# ---------------------------------------------------------------------------


def _compile_adapter_payload(card: ParsedCard) -> dict[str, Any]:
    """Build the provider adapter manifest payload."""
    fm = card.frontmatter
    name = str(fm["name"])
    payload: dict[str, Any] = {
        "name": name,
        "tip_kind": fm["tip_kind"],
        "provider_class": derive_provider_class_name(name),
        "capabilities": sorted(card.capabilities),
    }
    adapter_path = find_adapter_module(card)
    if adapter_path is not None:
        payload["adapter_module"] = str(adapter_path)
        adapter_caps = extract_adapter_capabilities(adapter_path)
        if adapter_caps is not None:
            payload["adapter_capabilities"] = sorted(adapter_caps)
    return payload


def derive_provider_class_name(slug: str) -> str:
    """Slug → ``{CamelCase}CredentialProvider`` per the spec.

    ``acme-llm`` → ``AcmeLlmCredentialProvider``.
    """
    parts = [p for p in slug.replace("_", "-").replace(".", "-").split("-") if p]
    camel = "".join(p[:1].upper() + p[1:] for p in parts)
    return f"{camel}CredentialProvider"


# ---------------------------------------------------------------------------
# Manifest output
# ---------------------------------------------------------------------------


def write_compiled(
    manifest: dict[str, Any],
    compiled_dir: Path,
    *,
    name: Optional[str] = None,
) -> Path:
    """Write a compiled manifest to ``<compiled_dir>/<name>.json``."""
    out_name = name or str(manifest["name"])
    compiled_dir.mkdir(parents=True, exist_ok=True)
    out = compiled_dir / f"{out_name}.json"
    out.write_text(json.dumps(manifest, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return out


__all__ = [
    "compile_card",
    "derive_provider_class_name",
    "write_compiled",
]
