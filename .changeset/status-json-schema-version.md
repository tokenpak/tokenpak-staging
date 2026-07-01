---
---

CLI: expose a stable schema version for `tokenpak status --json`.

The status command's machine-readable payload now carries an explicit
`schema_version` field backed by `STATUS_JSON_SCHEMA_VERSION`, so tests and
callers can distinguish the bounded JSON status contract from the richer
human-readable status output.
