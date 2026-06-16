# How to Update TokenPak

TokenPak provides CLI commands to update the proxy, CLI, and configuration from a centralized source.

## Quick Update

```bash
# Check if updates are available (no changes)
tokenpak update --check

# Apply updates
tokenpak update

# Preview what would change without applying
tokenpak update --dry-run
```

## Update Availability Notice

When you run `tokenpak claude` or `tokenpak codex`, TokenPak prints a one-line notice if a newer release is available on PyPI (for example, `TokenPak X.Y.Z available — run tokenpak update`). This check:

- runs only on the `tokenpak claude` / `tokenpak codex` launchers,
- is cached to at most once per day,
- is fail-open and offline-safe — network errors never delay or block the launcher, and
- can be disabled by setting `TOKENPAK_NO_UPDATE_CHECK=1`.

## What Gets Updated

| Component | Update Source | User Files Touched? |
|-----------|--------------|---------------------|
| CLI / Library | PyPI (`pip install --upgrade`) | No |
| Proxy (`proxy.py`) | Git / PyPI | Yes — core only |
| Config defaults | Git vault | No (user config preserved) |
| Lock file | Auto-updated after update | Yes |

## Update Flags

```bash
tokenpak update # Full update (proxy + config)
tokenpak update --check # Check for updates, don't install
tokenpak update --dry-run # Preview changes
tokenpak update --force # Update even if already up to date
tokenpak update --core-only # Skip config merge
```

## Update Flow

1. Check PyPI for latest `tokenpak` version
2. Download and install via `pip install --upgrade tokenpak`
3. If proxy was running → restart it
4. Update `~/.tokenpak/tokenpak.lock.json` with new version/hash

## Multi-Agent Environments

When multiple agents share a vault, all agents should run the same versions.

```bash
# On each agent machine:
tokenpak update
tokenpak version # verify all match
```

The lock file at `~/.tokenpak/tokenpak.lock.json` acts as the canonical version pin. Any agent with drift will warn on startup.

## Config Sync

To pull the latest config from the canonical vault/git source:

```bash
tokenpak config sync # sync from vault (git)
tokenpak config sync --dry-run # preview only
tokenpak config pull --source=url --url=https://example.com/tokenpak-config.json
```

## Rollback

Not yet automated. To rollback:
```bash
pip install tokenpak==0.3.0
# Restart proxy
```

Future: `tokenpak rollback <version>`

## Troubleshooting

See the troubleshooting guide for common issues.

Run `tokenpak doctor` for a full diagnostics report.
