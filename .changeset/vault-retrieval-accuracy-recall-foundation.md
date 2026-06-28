---
"tokenpak": minor
---

Companion: improve vault block retrieval accuracy for path-based lookups and
extend the recall storage foundation with Pak lifecycle and relation metadata.

`vault_retrieve(path=...)` now asks the proxy block endpoint to resolve the
source path directly before falling back to search, with ambiguity returned as
candidates instead of silently choosing a block.

Public-API additions (additive only; public-API snapshot contract):
- `tokenpak.companion.recall.PAK_RELATION_TYPES`
- `tokenpak.companion.recall.PAK_STATUS_VALUES`
- `tokenpak.companion.recall.PakRelationEntry`
- `tokenpak.companion.recall.store.PAK_RELATION_TYPES`
- `tokenpak.companion.recall.store.PAK_STATUS_VALUES`
- `tokenpak.companion.recall.store.PakRelationEntry`

The recall schema advances to v4 with nullable `paks.status`, relation helpers
for supersession/conflict edges, and no public-API removals.
