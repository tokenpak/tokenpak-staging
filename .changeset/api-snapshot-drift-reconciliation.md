---
---

Release-gate: reconcile the public-API snapshot (`tokenpak/_snapshots/public-api.json`).

The public-API snapshot ratchet check had been red and ignored (non-required on the
integration branch) since the snapshot was last regenerated for v1.7.0, providing zero
drift signal. Two compounding causes, both fixed here:

1. **Generator drift.** The integration branch's `gen_api_snapshot.py` lagged the
   canonical generator, missing: the released-surface **dispatch exclusion**
   (`tokenpak.orchestration.dispatch*` + `cli.commands.dispatch_cmd` — preview/source-only
   code excluded from the wheel, so it must not be recorded as released public API), the
   **third-party re-export denylist** (`_THIRD_PARTY_REEXPORTS`), and a host-independent
   import-error normalization fix. The canonical generator is adopted here, plus one new
   denylist entry — `vault.retrieval.vector_local.SentenceTransformer`, a lazily-bound
   (`= None`) third-party re-export kept for back-compat attribute access, the exact
   sibling of the already-excluded `faiss` re-export in the same module.

2. **Snapshot staleness.** The on-disk snapshot was regenerated and now reflects the
   genuine TokenPak-owned surface accumulated since v1.7.0 (cards subsystem, cards/config
   CLI commands, companion codex/stream, proxy ssrm/spend_guard/capture_intake, append-log,
   external-tool/gstack sources, etc.). Net: **+364 symbols, −3** (4310 → 4671 symbols
   excluding the generated_at timestamp).

removes-public-symbol: tokenpak.cli.commands.status.DB_DEFAULT — intentional; removed in
`a552a58c81` (legacy monitor.db migration: readers now route through the canonical path
resolver rather than this constant).
removes-public-symbol: tokenpak.proxy.server_extra.websocket_proxy.WebSocketServerProtocol —
third-party re-export (websockets library), never TokenPak-owned API; snapshot leak
correction via the re-export denylist.
removes-public-symbol: tokenpak.vault.retrieval.vector_local.faiss — third-party re-export
(faiss library), never TokenPak-owned API; snapshot leak correction via the re-export denylist.

No version impact: release-gate snapshot + generator hygiene only — no runtime or packaged
public API behavior change. Re-establishes the drift signal so future real public-API
changes are caught.
