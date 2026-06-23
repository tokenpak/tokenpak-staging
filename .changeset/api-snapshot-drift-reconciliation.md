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

3. **§3 internal-but-authored `__all__` curation (§2).** The
   `__module__`-based third-party sweep above cannot distinguish *TokenPak-authored-but-
   internal* symbols from *intended-public* ones, so a handful of internal helpers and
   cross-module re-exports were captured. They are scoped out **before** the contract is
   blessed (avoiding bless-then-remove churn) by declaring an explicit `__all__` on each
   owning module — environment-independent and correct regardless of regen host. Net for
   this step: **−12 symbols (4671 → 4659)**. Each removed symbol either remains public
   under its genuine owning module (cross-module re-export) or was never an intended public
   entrypoint (internal helper / env constant); see the `removes-public-symbol:` lines
   below.

removes-public-symbol: tokenpak.cli.commands.status.DB_DEFAULT — intentional; removed in
`a552a58c81` (legacy monitor.db migration: readers now route through the canonical path
resolver rather than this constant).
removes-public-symbol: tokenpak.proxy.server_extra.websocket_proxy.WebSocketServerProtocol —
third-party re-export (websockets library), never TokenPak-owned API; snapshot leak
correction via the re-export denylist.
removes-public-symbol: tokenpak.vault.retrieval.vector_local.faiss — third-party re-export
(faiss library), never TokenPak-owned API; snapshot leak correction via the re-export denylist.

# §3 internal-but-authored `__all__` curation (§2), 12 scope-outs:
removes-public-symbol: tokenpak.cli.commands.alerts.error — internal-but-authored; re-import
of `tokenpak.cli._messages.error` (a private CLI message-helper module already excluded from
the snapshot), never an intended public entrypoint.
removes-public-symbol: tokenpak.cli.commands.menu_status.json_snapshot — internal-but-authored;
internal JSON status helper consumed only by the internal `tokenpak._cli_core` via direct
attribute access (unaffected by `__all__`), never an intended public entrypoint.
removes-public-symbol: tokenpak.cli.commands.menu_status.snapshot — internal-but-authored;
internal convenience wrapper over `StatusCache.snapshot`, never an intended public entrypoint.
removes-public-symbol: tokenpak.creds.auth_mode.KIND_API_KEY — internal-but-authored;
re-import from owning module `tokenpak.creds.model` (remains public there), never an intended
public entrypoint here.
removes-public-symbol: tokenpak.proxy.ssrm.drift.canonicalize_user_turn — internal-but-authored;
re-import from owning module `tokenpak.proxy.ssrm.fingerprint` (remains public there), never an
intended public entrypoint here.
removes-public-symbol: tokenpak.proxy.ssrm.signals.Signals — internal-but-authored; re-import
from owning module `tokenpak.proxy.ssrm.contracts` (remains public there), never an intended
public entrypoint here.
removes-public-symbol: tokenpak.proxy.ssrm.signals.canonicalize_user_turn — internal-but-authored;
re-import from owning module `tokenpak.proxy.ssrm.fingerprint` (remains public there), never an
intended public entrypoint here.
removes-public-symbol: tokenpak.proxy.ssrm.signals.drift_score — internal-but-authored; re-import
from owning module `tokenpak.proxy.ssrm.drift` (remains public there), never an intended public
entrypoint here.
removes-public-symbol: tokenpak.proxy.ssrm.signals.open_state_db — internal-but-authored;
re-import from owning module `tokenpak.proxy.ssrm.state` (remains public there), never an
intended public entrypoint here.
removes-public-symbol: tokenpak.proxy.ssrm.signals.record_and_count — internal-but-authored;
re-import from owning module `tokenpak.proxy.ssrm.fingerprint` (remains public there), never an
intended public entrypoint here.
removes-public-symbol: tokenpak.telemetry.operational.rbac_auth.SNAPSHOT_GEN_ENV —
internal-but-authored; env-var-name constant read by release-gate snapshot generators, never an
intended public entrypoint.
removes-public-symbol: tokenpak.vault.chunk_shaping.skeleton_runtime_status — internal-but-authored;
internal diagnostic helper with no consumers anywhere in the package, never an intended public
entrypoint.

No version impact: release-gate snapshot + generator hygiene only — no runtime or packaged
public API behavior change. Re-establishes the drift signal so future real public-API
changes are caught.
