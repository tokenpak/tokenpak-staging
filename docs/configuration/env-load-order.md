# Environment & config load order — §2.4 specification

> **Status: SPEC ONLY.** This document specifies the precedence a later,
> separately-gated build will implement. The live runtime loaders
> (`tokenpak/core/config_loader.py`, `tokenpak/core/config.py`) are **not**
> wired to this chain yet — this page is the contract that build implements.
> `tokenpak config doctor` reports against this spec read-only and surfaces
> drift between the spec and the live loaders.
>
> Source design: centralized-env Packet B design (vault,
> `01_PROJECTS/tokenpak/design/centralized-env-packet-b-config-doctor-init-loadorder-design-2026-06-06.md`,
> §3). Variable inventory: the Packet-A env schema (vault,
> `01_PROJECTS/tokenpak/config/env-schema.md`).

## Scope

This spec covers **environment-variable resolution** — how a `TOKENPAK_*`
(or provider) key's effective value is chosen across env, `.env` files,
project config, user config, and built-in defaults. It harmonizes the two
precedence chains that exist today (`config_loader.get()` and
`core/config.get_config()`) into one documented order and inserts the
missing **`.env`-file layer**.

Throughout, `<tpk-home>` denotes the directory resolved by
`tokenpak._paths.home()` (`$TOKENPAK_HOME` override → `~/.tpk/` canonical →
`~/.tokenpak/` legacy fallback), and `<user-config>` denotes
`<tpk-home>/config.yaml`.

## Canonical precedence (highest wins)

```
1. CLI flag            (--config <path>, and per-key flags where defined)   -- highest
2. Process environment (os.environ -- already-exported TOKENPAK_* / provider keys)
3. Project .env        (./.env in the current working directory)
4. User env file       (<tpk-home>/.env -- mode 0600, gitignored)
5. [legacy fallback]   (<legacy-home>/.env, ONLY behind TOKENPAK_OPENCLAW_FALLBACK=1)   -- HELD
6. Project config      (TOKENPAK_CONFIG path, if set)
7. User config         (<tpk-home>/config.yaml, then config.json toggles)
8. Built-in defaults   (generate_default_yaml / per-key defaults)            -- lowest
```

**Relationship to live code.** Today's `config_loader.get()` implements
layers **2 → 7 → 8** (env → file → default). Today's
`core/config.get_config()` implements **2 → config.json → config.yaml → 8**.
This spec is a superset: it (a) makes the CLI flag (layer 1) explicit and
highest, (b) inserts the `.env`-file layers (3, 4) between process env and
config files, and (c) names the legacy fallback (5) as a gated, HELD layer.
Layers 6–8 are the existing file/default behavior, unchanged in order.

**Process env above `.env` (layers 2 > 3 > 4) is deliberate.** A value the
operator already exported for this process must win over a dotenv file, so
CI/sandbox/`$TOKENPAK_HOME`-override invocations are never silently
overridden by an on-disk `.env`. This matches the `_paths` precedence
philosophy (the operator `$TOKENPAK_HOME` override is highest there too) and
standard dotenv semantics (`.env` does not clobber an already-set env var).

**Ordering note vs the Packet-A schema doc.** The Packet-A `env-schema.md`
§2.4 lists "later wins" as OS env → `~/.tokenpak/.env` → `~/.openclaw/.env`
(fallback) → `./.env`, i.e. cwd `.env` highest among files. This spec
instead ranks **project `.env` (cwd) above user `<tpk-home>/.env`**
(layer 3 > 4), so a project-local override beats the per-user default — the
conventional precedence (more-specific/closer-to-invocation wins). This is
the one substantive refinement over the schema prose; reconciling
`env-schema.md` §2.4 is a build-time follow-up of the gated build.

## Resolution algorithm (specification, not code)

For a given key `K` with default `D`:

1. If a CLI flag binds `K` (e.g. `--config` selects the project-config
   file; a future per-key flag), that value wins.
2. Else if `K` is present in `os.environ`, that value wins.
3. Else consult `.env` files in order (project `./.env`, then
   `<tpk-home>/.env`, then — only if `TOKENPAK_OPENCLAW_FALLBACK=1` —
   `<legacy-home>/.env`); first hit wins. `.env` parsing is **additive**: a
   `.env` layer never overwrites a value already resolved at a higher layer.
4. Else consult config files: project config (if `TOKENPAK_CONFIG` is set),
   then `<user-config>` (`config.yaml`), then `config.json` toggles (for the
   toggle keys `core/config.py` owns).
5. Else return the built-in default `D`.

Type coercion (`int`/`bool`/`float`/`csv`) is applied **after** layer
selection, using the schema's declared `Format`, reusing the existing
`_bool_env` / `cast` semantics in `config_loader.get()`.

## Unknown-key handling (always-dynamic)

- An **unknown `TOKENPAK_*` key** present in env or `.env` is honored as a
  pass-through value and **never crashes** the loader. It is *reported* by
  `tokenpak config doctor` (check D4, `warn`) but resolution is graceful.
  There is no hardcoded master enum of accepted keys; the schema doc is
  documentation, not a gate.
- An **unknown key in a config file** is preserved (round-trips), not
  dropped — a newer config on an older binary degrades gracefully.
- The distinction vs `_paths.under()`'s fail-loud subdir contract is
  intentional: a typo'd home subdir creates junk *state on disk*; an unknown
  env var is just an ignored hint.

## User/system boundary

- The only writable user surfaces in the chain are `<tpk-home>/.env` and
  `<user-config>` — both user-owned tier, preserved across upgrades, never
  overwritten by the package installer.
- `<tpk-home>/.env` is mode `0600` and gitignored. Secret *values* live
  **only** there: never in `config.yaml`, never in `.env.example`, never in
  any committed surface. `tokenpak config doctor` masks always — it reports
  secret presence by name only and never prints a value.
- Every path in the chain resolves through `tokenpak._paths` (`home()`,
  `legacy_home()`), never a re-hardcoded `~/.tokenpak/`. The gated build
  must repoint `config_loader.CONFIG_PATH` and `core/config.CONFIG_PATH`
  onto `_paths` as part of landing this spec (or explicitly defer with a
  tracked note).

## Explicitly HELD (not buildable under this spec)

Each of the following requires a separate explicit security/ops ruling and
is named here only to bound the spec:

- **Layer 5** — the `<legacy-home>/.env` OpenClaw fallback behind
  `TOKENPAK_OPENCLAW_FALLBACK=1`. The spec fixes its *position* (strictly
  below all first-class `.env` files, flag-gated, off by default); the
  fallback reader build is held.
- **`migrate-from-openclaw`** — out of scope entirely.
- **Mutating the live runtime loaders** to adopt this chain — held until
  the security/ops ruling, since it changes production config resolution.

## Build-time follow-ups this spec implies (for the gated build)

1. Add `.tpk/` to `.gitignore` (sibling to the existing `.tokenpak/`).
2. Repoint `config_loader.CONFIG_PATH` and `core/config.CONFIG_PATH` at
   `_paths.home()`.
3. Move the ad-hoc `~/.tokenpak/.env` writer in `_cli_core.py` onto
   `<tpk-home>/.env` via `_paths`.
4. Reconcile vault `env-schema.md` §2.4 ordering with this spec
   (project `.env` > user `.env`).
