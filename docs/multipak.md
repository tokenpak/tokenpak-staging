# MultiPak Pro — Phase 1 OSS surface

> **Phase 1 status**: scaffolding for the future Pro daemon. Read-only Vault Pak inspection works without Pro; everything else returns a clear "Pro daemon required" message until you install `tokenpak-paid` (Pro).

## What MultiPak is

MultiPak Pro is the local-first cross-platform AI context-continuity layer for TokenPak. It captures useful AI work into local **Paks**, recalls the right Paks across time and tools, packages them for the current AI session, hydrates exact details when needed, and lets you continue work across platforms without rebuilding context manually.

> Start anywhere. Continue anywhere. MultiPak brings the right Paks into the current AI session without dumping the full history.

The full feature set is **Pro** (closed-source local Rust daemon, gated by the relevant standard). Phase 1 is what TokenPak ships **today** in OSS — the contracts, the Vault Pak adapter, the read-only inspection surface, and the daemon hooks.

## Pak taxonomy (the relevant standard)

5 canonical subtypes:

| Subtype | Authority | Phase 1 OSS support |
|---|---|---|
| **Vault Pak** | `file_source` | ✅ full (read-only via `tokenpak.vault.pak_adapter`) |
| **Interaction Pak** | `tool_result` / `llm_generated` | ⚠️ stub-only (promotion needs Pro daemon) |
| **Decision Pak** | `user_approved` | ❌ Pro-only |
| **Recall Pak** | derived | ❌ Pro-only (recall ranking is the daemon's job) |
| **Handoff Pak** | composite | ❌ Pro-only (target-platform packaging) |

## CLI — `tokenpak pak`

```bash
$ tokenpak pak status
MultiPak Pro Phase 1 status
───────────────────────────
❌ Daemon state : unavailable
⚠️ multipak.enabled : False
⚠️ Pak store present : False
📦 Vault Paks indexed : 15922
📦 Promotion candidates : 0

ℹ️ Pro daemon not installed — Vault Pak inspection still works via the OSS
 adapter. Install tokenpak-paid for the full surface.
```

`tokenpak pak status --json` emits the same JSON shape as `GET /pak/v1/status`:

```json
{
 "daemon_state": "unavailable",
 "multipak_enabled": false,
 "pak_store_present": false,
 "vault_paks_indexed": 15922,
 "promotion_candidates": 0
}
```

### Subcommands

| Command | OSS support | Notes |
|---|---|---|
| `tokenpak pak inspect <pak-id-or-file>` | Vault: ✅, others: ❌ | `--json` for machine output |
| `tokenpak pak export <pak-id> -o <dir>` | Vault: ✅, others: ❌ | Vault Paks export to `pak.json` |
| `tokenpak pak import <dir> -o <pak>` | ❌ Pro | Capture pipeline is Pro-only |
| `tokenpak pak status` | ✅ always | Diagnostic; never errors |

Exit codes follow the relevant standard: `0` success, `1` user-facing error (missing Pak, Pro required), `2` argparse usage error.

### Pak ID format

Vault Paks: `vault:<source-path>#<sha256-prefix>` — for example, `vault:<project>/README.md#abc12345`. The `#` is significant — when used in URLs (e.g., `/pak/v1/inspect/<pak-id>`), percent-encode it as `%23`.

Other subtypes (Pro): `interaction:<session>:<entry>`, `decision:<id>`, `recall:<query-hash>`, `handoff:<target>:<id>`.

## Recall Preview + Apply — `tokenpak pakplan` (OSS, vault file index)

The `pakplan` surface turns vault recall into a **preview → review → apply** workflow so you stop copy-pasting context by hand and stop dumping the whole history into a session.

```bash
# 1. Preview recall candidates ranked over the vault FILE index (BM25)
$ tokenpak pakplan recall --query auth --limit 10
PAKPlan recall preview (OSS — vault file index, BM25)
   1. vault:gamma-runbook  Gamma auth runbook
      source : file/file_source  score: 9.4
      snippet: auth incident escalation
      risk   : warn
   2. vault:alpha-design   Alpha auth design
      source : code/file_source  score: 4.1
      snippet: how auth tokens rotate

# 2. Apply a selection — keep some, drop the rest, and record why
$ tokenpak pakplan apply --query auth --exclude vault:gamma-runbook --json
{ "included": [ ... ], "dropped": [ {"pak_id": "vault:gamma-runbook", "drop_reason": "user_excluded", ...} ],
  "boundary": "oss_vault_file_index", "proof_path": ".../selections/sel-….json", ... }
```

- `recall` previews candidates with **source**, **rank**, **snippet/title**, **BM25 score**, and the vault block's **risk** field. `--query` is a BM25 query over the vault file index; `--project` / `--type` are client-side filters over the returned candidates.
- `apply` partitions candidates into an **include/drop proof**: `--include id …` keeps only those (the rest are dropped as `not_selected`), or `--exclude id …` drops those (the rest are included). `--dry-run` shows the proof without writing it; `--reason` records a note.
- The proof is written under the companion `selections/` home as an inspectable, reversible JSON artifact — delete the file to undo. Its `included` / `dropped` lists are exactly what **Receipt v1** surfaces as the request's context proof.

> **OSS boundary (the relevant standard).** OSS ranked recall is confined to the **vault file index** — BM25 over your vault files — carrying the honest `boundary: "oss_vault_file_index"` marker on every proof. OSS never auto-applies a selection to a live request. **Ranked retrieval over the recall / Pak store** (`paks` / `paks_fts` / `pak_relations`), **cross-source scoring, learned ranking, capture, and anchor hydration are Pro** and run behind the daemon — `POST /pak/v1/recall` stays `501` (see below). The OSS slice is vault-index retrieval + selection + proof; Pak-store recall ranking is not shipped in OSS.

## HTTP — `/pak/v1/*`

The proxy exposes a separate `/pak/v1/*` namespace from the existing `/tpk/v1/*` OSS app API. Auth is the same: localhost-only, optional `X-TPK-Key` header.

### `GET /pak/v1/status`

Always works. Same JSON payload as `tokenpak pak status --json`.

### `GET /pak/v1/inspect/<pak-id>`

- `vault:` IDs → 200 with the Pak's serialized form (via the OSS adapter)
- Other subtypes → 501 `not_implemented` with `reason: "pro_daemon_required"`
- Unknown vault block → 404 `pak_not_found`

### `POST /pak/v1/recall`

Always 501 in Phase 1 — recall ranking is Pro-only (the relevant standard row 8).

### Standardized 501 envelope

Every Pro-gated endpoint returns this shape, so clients can treat `error == "not_implemented"` + `reason` as the canonical "daemon absent" signal:

```json
{
 "error": "not_implemented",
 "reason": "pro_daemon_required",
 "detail": "<human message>",
 "suggested_action": "Install tokenpak-paid (Pro) to enable this surface.",
 "daemon_state": "unavailable"
}
```

`daemon_state` mirrors the relevant standard telemetry. Phase 1 only emits `"active"` or `"unavailable"`. Phase 2 adds `"tip_mismatch"` and the four state-machine values (`offline-grace`, `offline-expired`, `user-revoked`, `billing-grace`).

## Configuration — `pro.multipak.enabled`

Default `false` per [ ](../standards/32-multipak-pro-architecture.md) (opt-in until 1-week soak post-bootstrap).

```yaml
# ~/.tokenpak/config.yaml
pro:
 multipak:
 enabled: false # set true once Pro is installed and you've completed soak
```

The OSS read-only path (Vault Pak inspection, `/pak/v1/status`) **works regardless** of this flag. The flag mainly governs whether the daemon is consulted at all.

## Companion journal coexistence

Per the relevant standard the OSS companion journal continues to auto-capture every prompt — local-only, no upload. This is the existing entry point per the [companion guide](../tokenpak/companion/GUIDE.md). Promotion of a journal entry to a MultiPak Interaction Pak is the **opt-in step**:

```python
from tokenpak.companion.journal.pak_aware import (
 mark_promotion_candidate,
 list_promotion_candidates,
)
from pathlib import Path

db = Path.home() / ".tokenpak/companion/journal.db"

# Mark an entry as ready for daemon-side promotion
mark_promotion_candidate(db, entry_id=42)

# List candidates the daemon should consider
for entry in list_promotion_candidates(db, session_id="my-session"):
 print(entry.entry_id, entry.entry_type, entry.content[:80])
```

Phase 1 OSS code never auto-promotes. The Pro daemon (Phase 2+) consumes this surface to enumerate entries it should consider for Interaction Pak promotion.

## Privacy contract (the relevant standard)

**No memory content ever crosses the license-validation boundary** (the relevant standard). The Pak schema is structurally disjoint from license-payload field prefixes (`license_token`, `tenant_id`, `fingerprint`, `issuer`, `signature`) — enforced by the Phase 0 contract tests and the [`09 §3.11.b`](../standards/09-audit-rubric.md) quarterly audit.

Pak content stays local. The license refresh request carries only the license token, the per-install ed25519 public key, and the hardware-bound machine fingerprint — never Paks, anchors, prompts, completions, or telemetry.

## Phasing

| Phase | Surface | Status |
|---|---|---|
| **0** | TIP capability constants + Pak/ContextPackage contracts | ✅ shipped (PR #101 / registry PR #4) |
| **1** | Vault Pak adapter + Pak-aware journal + `tokenpak pak` CLI + `/pak/v1/*` stubs | ⏳ this PR |
| 2 | Capture pipeline + recall + ranking + SQLite FTS | gated by the relevant standard |
| 3 | Context Package builder + Handoff Pak + VS Code + MCP adapters | gated |
| 4 | Anchor Hydration + coverage scoring + audit log | gated |
| 5 | Encrypted store + retention engine + dashboard surfaces | gated |
| 6 | Embeddings (deferred per Decision #8) + supersession + auto-promotion | gated |

## References

- [Standard 32 — MultiPak Pro Architecture](../standards/32-multipak-pro-architecture.md) — the canonical authority for everything in this doc
- [Standard 25 — Pro Tier Architecture](../standards/25-pro-tier-architecture.md) — daemon process model, license registry, fallback contract
- [Standard 31 — TIP Versioning Strategy](../standards/31-tip-versioning-strategy.md) — capability negotiation rules
- [Standard 23 — Provider Adapter Standard](../standards/23-provider-adapter-standard.md) — additive-only contract, offline test convention
