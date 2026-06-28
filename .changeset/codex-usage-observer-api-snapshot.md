---
---

Release-gate: ratchet the public-API snapshot for the Codex usage observer and
exec-capture surface.

This records the intentional additive companion surface introduced by
`tokenpak.companion.codex.usage`: safe session JSONL token-count parsing,
latest-session selection, HMAC event identifiers, and run-scoped `codex exec
--json` usage sidecar capture. No public symbols are removed.
