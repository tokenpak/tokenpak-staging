# SPDX-License-Identifier: Apache-2.0
"""tokenpak.agent.license — deprecated compatibility shim.

This subtree predates the tokenpak.licensing subsystem, which is the
canonical, actively-maintained home for tier/feature-gating logic
(see tokenpak.licensing.is_feature_enabled). The symbols below are kept
only for backward-compatibility with any external caller that may still
import this path directly; no code in this codebase imports from here.
Do not add new dependents — use tokenpak.licensing instead.
"""

import warnings as _warnings

from .validator import TIER_FEATURES, LicenseTier, required_tier_for

_warnings.warn(
    "tokenpak.agent.license is deprecated and unused internally; "
    "use tokenpak.licensing instead. This module will be removed in a "
    "future release.",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = ["LicenseTier", "TIER_FEATURES", "required_tier_for"]
