---
"tokenpak": minor
---

Companion: configurable generic memory-source ingestion ("bring your own
knowledge base").

Adds a generic path so the companion can ingest lessons from any directory of
Markdown notes, without requiring the multi-agent vault directory layout.

Public-API additions (additive only; public-API snapshot contract):
- `tokenpak.companion.memory.lesson_ingest.ingest_from_dir`
- `tokenpak.companion.memory.lesson_ingest.ingest_sources`

New user surface: `tokenpak companion ingest --memory-dir <path>` /
`tokenpak companion status` + `TOKENPAK_COMPANION_MEMORY_DIRS` env var.
`CompanionConfig.memory_dirs`. Existing `ingest_from_vault` behavior is
unchanged. No public-API removals in this change.
