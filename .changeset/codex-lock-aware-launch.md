---
"tokenpak": patch
---

Codex companion launches now preflight both Codex local SQLite databases before
starting a shared-home session. A locked `logs_2.sqlite` now receives the same
TokenPak-guided remediation as a locked `state_5.sqlite`, instead of surfacing a
raw Codex `database is locked` startup failure.

When Codex companion setup is already current, normal launches also avoid
re-running Codex CLI setup probes. `tokenpak codex --install-only` remains the
explicit repair path for refreshing MCP registration and hook feature setup.
