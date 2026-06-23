"""Product wordmark + dynamic license-tier badge.

Single source of truth for rendering the ``TokenPak`` wordmark with the
active license tier appended as a badge (e.g. ``TokenPak PRO``). Tier is read
exclusively through the OSS :mod:`tokenpak.licensing` module — there is **no**
dependency on the paid package, so this is safe to call from any OSS surface
(companion banner, CLI headers) per the OSS/Pro boundary.

Design contract:

* **Free renders plain** ``TokenPak`` — no badge, ever.
* **Pro / Team / Enterprise** render an uppercase tier badge: ``TokenPak PRO``,
  ``TokenPak TEAM``, ``TokenPak ENTERPRISE``.
* **Fail-closed and silent.** Any error (licensing import failure, malformed
  license, unexpected tier) degrades to a plain ``TokenPak`` wordmark. This
  path is rendered on every banner/header, so it must never raise.
* **Single-colour in CLI.** Callers must not apply a distinct colour to the
  badge (Brand Style Guide: the CLI wordmark is single-colour; the split-teal
  treatment is reserved for display surfaces). This module returns plain text
  and leaves styling to the caller.
"""

from __future__ import annotations

_PRODUCT = "TokenPak"

# License statuses under which a paid tier still "counts" for display purposes.
# Anything else (expired, revoked, …) falls back to a plain wordmark.
_BADGE_STATUSES = frozenset({"active", "pending_validation"})


def tier_badge() -> str:
    """Return the uppercase tier badge, or ``""`` for Free / on any error.

    Examples: ``"PRO"``, ``"TEAM"``, ``"ENTERPRISE"``. Never raises. Reads the
    local license once via the OSS licensing module — no network call, no paid
    dependency.
    """
    try:
        from tokenpak import licensing as _lic

        lic = _lic.load_license()
        tier = getattr(lic, "tier", _lic.TIER_FREE)
        if tier == _lic.TIER_FREE:
            return ""
        if getattr(lic, "status", "active") not in _BADGE_STATUSES:
            return ""
        label = _lic.describe_tier(tier)  # "Pro" / "Team" / "Enterprise"
        return label.upper()
    except Exception:
        return ""


def product_label(*, upper: bool = False) -> str:
    """Return the product wordmark with the active tier badge appended.

    ``Free  -> "TokenPak"``      (``upper=True`` -> ``"TOKENPAK"``)
    ``Pro   -> "TokenPak PRO"``  (``upper=True`` -> ``"TOKENPAK PRO"``)
    ``Team  -> "TokenPak TEAM"``, ``Enterprise -> "TokenPak ENTERPRISE"``.
    """
    name = _PRODUCT.upper() if upper else _PRODUCT
    badge = tier_badge()
    return f"{name} {badge}" if badge else name
