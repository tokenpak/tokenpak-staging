# Vault Scheduling — User Guide

TokenPak's **vault scheduling** lets you register directories that should be kept indexed for context injection, and reindex them on demand with a single CLI verb — no agent runtime, no cron scripts, no shell glue.

This is the canonical, product-first workflow for keeping your vault fresh.

> **Status:** Available since the VDS (Vault Directory Scheduling) initiative landed in TokenPak v1.4.x.
> **Authoritative spec:** `tokenpak/vault/config.py` (schema v1) + `tokenpak vault repair` + `tokenpak index --reindex-all|--reindex-path`.

---

## How it works (in 30 seconds)

1. You list the directories you want indexed in `~/.tokenpak/vault.yaml` (schema v1).
2. `tokenpak index --reindex-all` walks every registered path and reindexes it in one pass.
3. `tokenpak index --reindex-path <path>` reindexes a single registered path.
4. `tokenpak doctor` (and `tokenpak vault repair`) read the same config and flag stale, missing, or corrupt entries.

No agent runtime is required. No external cron scripts are required. If you want recurring reindex, wrap `tokenpak index --reindex-all` in a system cron or systemd timer — the CLI is the contract.

---

## `~/.tokenpak/vault.yaml` (schema v1)

```yaml
version: 1
paths:
  - path: /home/me/notes
    schedule: daily
  - path: /home/me/projects/active
    schedule: hourly
```

Fields:

- **`path`** — absolute filesystem path to index. Required.
- **`schedule`** — advisory cadence label (`hourly`, `daily`, `weekly`, or `manual`). TokenPak does NOT run a scheduler itself; this label is for your own cron/timer/orchestrator to read.

Add an entry by editing the file directly, or — on the paid tier — via `tokenpak vault add <path> --schedule daily`.

---

## CLI verbs

### `tokenpak index --reindex-all`

Reindex every directory registered in `vault.yaml`. Honors `--workers`, `--budget`, and the other `tokenpak index` flags.

```bash
tokenpak index --reindex-all
```

### `tokenpak index --reindex-path <path>`

Reindex a single registered directory. The path must already appear in `vault.yaml`; this guards against accidentally indexing arbitrary directories under the vault-scheduling banner.

```bash
tokenpak index --reindex-path /home/me/notes
```

### `tokenpak vault repair`

Walk the vault index, identify stale or corrupted entries, and rebuild them.

```bash
tokenpak vault repair
```

### `tokenpak doctor`

Among other checks, `doctor` reads `vault.yaml`, runs a staleness check against the on-disk index for each registered path, and reports findings. Useful as a pre-flight before a reindex.

```bash
tokenpak doctor
```

---

## Migration note — deprecating agent-side rebuild cron scripts

Before VDS landed, many TokenPak users (and the OpenClaw fleet) shelled out to per-host scripts to rebuild the index on a schedule. Those scripts are now **deprecated** in favor of `tokenpak index --reindex-all`. Reasons:

- **Single source of truth.** `vault.yaml` is the registry; the CLI reads it directly. Agent-side scripts duplicated that list and drifted.
- **Product-first.** The reindex workflow should not require running an agent runtime.
- **Auditable.** `tokenpak doctor` and `tokenpak vault repair` operate over the same registry, so health checks and rebuilds use the same data.

If you have a host-local cron script that hardcodes vault paths, replace it with:

```cron
0 * * * * tokenpak index --reindex-all
```

…and move any per-path overrides into `vault.yaml` `schedule:` annotations + a thin dispatcher script that calls `tokenpak index --reindex-path` per path.

---

## FAQ

**Q: Does TokenPak run a scheduler itself?**
No. `schedule:` in `vault.yaml` is advisory. Use system cron, systemd timers, or your existing orchestrator. The CLI is the contract.

**Q: What if a registered path no longer exists on disk?**
`tokenpak doctor` will report it; `tokenpak vault repair` can be used to scrub stale entries from the index.

**Q: Can I register a path via CLI on the OSS tier?**
The OSS CLI exposes `tokenpak index --reindex-all|--reindex-path` and `tokenpak vault repair`. To add or remove registered paths from the CLI, the paid tier provides `tokenpak vault add|list|remove`. OSS users edit `vault.yaml` directly.

**Q: Is there a recommended cadence?**
Hourly for active project trees, daily for notes/reference vaults. Watch mode (`tokenpak index <dir> --watch`) is an alternative for high-churn directories.
