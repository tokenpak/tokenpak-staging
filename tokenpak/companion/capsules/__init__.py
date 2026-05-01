"""
tokenpak.companion.capsules
===========================

Companion-side memory capsules and request-body capsule helpers.
"""

from .builder import CapsuleBuilder, SessionCapsule, load_capsule, save_capsule  # noqa: F401
from .retention import (  # noqa: F401
    ACTIVE_CAPSULE_NAME,
    CapsuleRetentionResult,
    apply_capsule_retention,
    capsule_build_enabled,
    refresh_active_capsule,
)

__all__ = [
    "ACTIVE_CAPSULE_NAME",
    "CapsuleBuilder",
    "CapsuleRetentionResult",
    "SessionCapsule",
    "apply_capsule_retention",
    "builder",
    "capsule_build_enabled",
    "load_capsule",
    "refresh_active_capsule",
    "save_capsule",
]
