# Hermes Adapter — Specification & Stub

Hermes is a routing target served through a TokenPak format adapter. This
document specifies the Hermes adapter contract and describes the **stub** that
ships today.

> **Status: contract stub.** The shipped `HermesAdapter` declares its TIP
> capability contract but **cannot route real traffic**. Enabling live routing
> is a separate, explicit activation step. The stub exists so adapter authors
> and tests have a concrete contract to build against without any risk of the
> proxy silently sending traffic to an unfinished adapter.

## What the stub guarantees

`tokenpak.proxy.adapters.hermes_adapter.HermesAdapter`:

- **Declares a valid contract.** `source_format = "hermes"`,
  `tip_min_version = "TIP-1.0"`, `tip_max_version = "TIP-1.x"`, and a non-empty
  `capabilities` set. `capability_contract()` validates against the asserted
  TIP version.
- **Never auto-detects.** `detect(...)` always returns `False`, so the registry
  never selects the stub for a live request.
- **Fails loud on every routing path.** `normalize`, `denormalize`, and
  `get_default_upstream` raise `NotImplementedError` with a clear message.
- **Is not registered by default.** `build_default_registry()` does not include
  Hermes, so it is absent from the active adapter set.

Together these mean the stub **cannot silently route real traffic**: it is
never matched, never registered, and every routing entry point fails loud.

## Declared capabilities

The stub declares the capabilities a concrete Hermes adapter is expected to
support:

| Capability | Label | Purpose |
|---|---|---|
| Route class | `tip.route.class.v1` | exposes request content type for route-class policy |
| Telemetry attribution | `tip.telemetry.attribution.v1` | per-source savings attribution in telemetry |

A concrete implementation may declare additional capabilities; it must keep
these two unless the contract is revised.

## TIP version range

Hermes supports `TIP-1.0` through `TIP-1.x` (inclusive) — any minor version
within TIP-1. See [Adapter Authors Guide](adapter-authors.md) for the version
label rules and the full validation behavior.

## Implementing a concrete Hermes adapter

To turn the stub into a working adapter:

1. Implement `detect`, `normalize`, `denormalize`, and `get_default_upstream`
   on a `FormatAdapter` subclass (or replace the stub's bodies).
2. Keep or extend the declared `capabilities`, and keep the TIP version range
   accurate.
3. Register the adapter in the active registry with an appropriate priority,
   as part of the activation step — not in this contract surface.
4. Confirm `run_startup_self_test()` reports the adapter in `passed`.

Until those steps are complete, the stub remains in place and the proxy treats
Hermes as unavailable rather than routing to a partial implementation.
