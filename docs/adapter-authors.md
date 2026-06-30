# Adapter Authors Guide — TIP Capability Contract

This guide explains how a format adapter declares its **TIP capability
contract**: the TIP protocol version range it supports and the optimization
capabilities it publishes. The proxy validates this contract when an adapter
is loaded and **gates out** any adapter that is incompatible with the running
proxy *before* it can serve traffic.

## The contract

Every adapter declares three things, as class attributes on its
`FormatAdapter` subclass:

| Attribute | Type | Default | Meaning |
|---|---|---|---|
| `capabilities` | `frozenset[str]` | `frozenset()` | TIP optimization capability labels this adapter supports. |
| `tip_min_version` | `str` | `"TIP-1.0"` | Lowest TIP version the adapter supports (inclusive). |
| `tip_max_version` | `str` | `"TIP-1.x"` | Highest TIP version the adapter supports (inclusive). |

```python
from tokenpak.proxy.adapters.base import FormatAdapter
from tokenpak.tip.capabilities import (
    TIP_COMPRESSION_V1,
    TIP_TELEMETRY_ATTRIBUTION_V1,
)


class MyProviderAdapter(FormatAdapter):
    source_format = "my-provider"
    tip_min_version = "TIP-1.0"
    tip_max_version = "TIP-1.x"
    capabilities = frozenset({
        TIP_COMPRESSION_V1,
        TIP_TELEMETRY_ATTRIBUTION_V1,
    })

    # ... detect / normalize / denormalize / get_default_upstream ...
```

The defaults are deliberately permissive: an adapter that omits the version
attributes supports any minor version within TIP-1, which is what most
adapters want.

## TIP version labels

TIP versions use the label form `TIP-MAJOR.MINOR` and have **no patch
segment**. Patch-level product fixes live in the package version, not in the
TIP compatibility label.

- `TIP-1.0` — a concrete version.
- `TIP-1.x` — the **minor wildcard**: any minor version within major 1. Use it
  as `tip_max_version` to mean "every current and future TIP-1 minor".

Ranges are **inclusive** on both ends. The proxy normalizes the `.x` shorthand
to an internal upper bound when comparing; your manifest keeps the shorthand.

## Capability labels

Capability labels come from the optimization vocabulary in
`tokenpak.tip.capabilities` (for example `tip.compression.v1`,
`tip.cache.proxy-managed`, `tip.telemetry.attribution.v1`). A vendor may also
declare an external capability using the reserved `ext.<vendor>.<feature>`
namespace, e.g. `ext.acme.turbo`.

A capability label that is neither a recognized `tip.*` label nor a valid
`ext.*` label is rejected by the contract check (see failure modes below).

## How the contract is validated

The contract for an adapter is available via `adapter.capability_contract()`,
which returns an `AdapterCapabilityContract`. You can validate it directly:

```python
from tokenpak.tip.adapter_contract import (
    AdapterCompatibilityError,
    validate_adapter_compatibility,
)

contract = MyProviderAdapter().capability_contract()
try:
    validate_adapter_compatibility(contract)
except AdapterCompatibilityError as exc:
    # public-safe message; handle / log / surface
    ...
```

At proxy startup the registry runs `run_startup_self_test()`, which validates
every registered adapter against the asserted TIP version:

- a **compatible** adapter stays in the active registry and is reported in
  `result.passed`;
- an **incompatible** adapter is **removed** from the active registry (it can
  never route traffic), is reported in `result.gated_out` with a public-safe
  reason, and a warning is logged.

The self-test runs at startup only; it never enables routing for an adapter
that was not already registered and wired.

## Failure modes

`validate_adapter_compatibility` raises `AdapterCompatibilityError` — a
**fail-loud**, public-safe error (it names the adapter, the version labels, and
the offending capability labels only) — in these cases:

| Failure | Example | Why |
|---|---|---|
| Version below the adapter's minimum | proxy asserts `TIP-1.0`, adapter sets `tip_min_version="TIP-2.0"` | the adapter needs a newer protocol than the proxy speaks |
| Version above the adapter's maximum | proxy asserts `TIP-1.0`, adapter sets `tip_max_version="TIP-0.9"` | the adapter only speaks an older protocol |
| Malformed version label | `"TIP-1"`, `"1.0"`, `"TIP-1.2.3"`, `"TIP-x.0"` | not a valid `TIP-MAJOR.MINOR` / `TIP-MAJOR.x` label |
| Unknown capability label | `"tip.not.a.real.label"` | not in the optimization vocabulary and not an `ext.*` label |

A malformed or unsupported value is never silently ignored — the contract
fails loud so the problem surfaces at load time rather than mid-request.
