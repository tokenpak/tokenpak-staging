"""tokenpak.agent — deprecated compatibility subtree.

Everything under this package is leftover debris from completed migrations:
tier/feature-gating logic moved to tokenpak.licensing, and the `serve`/
`metrics` CLI command implementations moved to tokenpak.cli.commands.
Nothing in this codebase imports from this subtree; the remaining submodules
(agent.license, agent.cli.commands.serve, agent.cli.commands.metrics) are
thin re-export shims kept only for backward compatibility with any external
caller that may still import these paths directly.

This `__init__.py` (and the ones in agent/cli and agent/cli/commands) exists
so this package is a regular (non-namespace) package: that makes it visible
to the import-linter architecture-layers contract, which governs it as the
outermost layer so it may depend on tokenpak.cli and below but nothing may
depend on it. Do not add new code here — use the canonical locations above.
"""

import warnings as _warnings

_warnings.warn(
    "tokenpak.agent is deprecated and unused internally; its remaining "
    "submodules are compatibility shims for tokenpak.licensing and "
    "tokenpak.cli.commands. This package will be removed in a future "
    "release.",
    DeprecationWarning,
    stacklevel=2,
)
