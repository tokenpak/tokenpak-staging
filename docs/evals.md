# Deterministic Eval Mode — `[TIP: deterministic=on]`

Reproducible evaluation traffic through the TokenPak proxy. Prepend the
directive to the first user message of a request and the proxy disables every
output-changing behavior it controls, then emits explicit reproducibility
metadata so eval harnesses can verify what was actually sent.

Deterministic eval mode is **OSS** — it is a trust/credibility feature, not a
Pro tier.

```
[TIP: deterministic=on] <your prompt>
```

> Spec home: Standard 31 (TIP versioning strategy) — deterministic-directive
> section is a DRAFT amendment pending ratification
> (`standards/_amendments/2026-06-11-std-31-deterministic-directive-section.md`).
> Source contract: Proposal-4 Packet 1B.

---

## What deterministic mode guarantees

| Behavior | Normal mode | Deterministic mode |
|---|---|---|
| Transparent upstream retries (502/503/504, protocol errors) | up to `TOKENPAK_UPSTREAM_RETRIES` attempts | **one attempt — failures surface to the client (fail loud)** |
| Cross-provider fallback | n/a on the proxy request path | **none — route locked to the client-addressed provider/model** |
| Semantic response substitution (`tokenpak.cache.semantic_cache`) | near-duplicate responses may be served from cache | **disabled — forced miss (`deterministic_bypass`); eval traffic neither reads nor populates the cache** |
| Vault context injection (prompt mutation) | may splice context into the prompt | **disabled** |
| Compression request hook (prompt mutation) | may rewrite the request body | **disabled** |
| DLP secret redaction (`TOKENPAK_DLP_MODE=redact`) | mutates body silently | **stays active (security control) but is explicitly recorded** in `Adapter-Required-Transform` and the mutation-delta header |
| Seed / generation params | forwarded as-is | forwarded as-is and **echoed in metadata headers**; never injected (no provider, including Anthropic, is assumed to support `seed`) |
| Orchestration-level `RetryEngine` (agentic workflows) | 5-level escalation (retry → model downgrade → provider switch → handoff → alert) | **single attempt; failure saves state, alerts, raises `RetryExhaustedError`** (construct with `RetryEngine(..., deterministic=True)`) |

Unsupported deterministic fields **fail loudly, never silently strip**: an
unrecognized value (e.g. `deterministic=maybe`) is rejected with HTTP 400
`error.type=tokenpak_deterministic_invalid_value` before any provider send.

## Precedence vs other TIP directives

| Combination | Behavior |
|---|---|
| `[TIP: deterministic=on estimate=on]` | **Compatible.** Estimate is side-effect-free (returns RiskEstimate JSON, no provider call). The estimate response carries `X-TokenPak-Deterministic: on` and the request fingerprint. |
| `[TIP: deterministic=on allow=once]` (also `allow=15m`, `allow=session`, `allow=<N>`) | **Incompatible — rejected** with HTTP 400 `error.type=tokenpak_deterministic_directive_conflict`. Allow semantics release/replay a previously held request; the replayed body is not the one carrying the directive and the approval round-trip is interactive multi-turn state — both break reproducibility. Neither directive is silently stripped. |
| `[TIP: deterministic=on bypass=on]` | **Incompatible — rejected** (same 400). Deterministic mode is explicitly not a spend-band override. |
| `[TIP: deterministic=on cancel]` | Compatible (cancel never reaches a provider). |
| `max=` / `ttl=` alongside `deterministic=on` (without allow/bypass) | Compatible-inert (those keys only act with allow/bypass). |
| Spend guard bands | **Unchanged in both directions.** `deterministic=on` never relaxes a block/hard-block; a spend block does not disable deterministic semantics — the held request is simply not sent (the 402 carries the deterministic marker + fingerprint). |

The canonical precedence contract is
`tokenpak.proxy.spend_guard.policy.deterministic_precedence`.

## Request fingerprint

`X-TokenPak-Deterministic-Fingerprint: sha256:<hex>` — SHA-256 over the
canonicalized request body:

1. Strip the `[TIP: ...]` header (the fingerprint identifies the semantic
   request the provider sees, so annotated and bare replays match).
2. Parse the body JSON; drop the **top-level strip-list keys**
   (case-insensitive):

   | Key | Why stripped |
   |---|---|
   | `metadata` | client telemetry (e.g. Anthropic `metadata.user_id`) |
   | `stream` | transport choice — same logical request streamed or not |
   | `nonce` | volatile anti-replay value |
   | `timestamp` | volatile clock value |
   | `request_id` | volatile correlation id |
   | `idempotency_key` | volatile retry-safety token |

3. Re-serialize with sorted keys, compact separators, UTF-8 (no ASCII
   escaping); hash with SHA-256.

Auth never appears in the request body (credentials travel in HTTP headers,
which are not hashed), so it is excluded by construction. The strip-list is
defined in code at
`tokenpak.proxy.spend_guard.tip_header.FINGERPRINT_STRIP_FIELDS`.

The fingerprint is computed over the **final forwarded bytes** — if a
recorded transform (DLP redaction) mutated the body, the fingerprint reflects
what was actually sent and the mutation is visible in the delta header below.

## Metadata headers

Emitted on every deterministic response (streaming and non-streaming,
including normalized upstream errors):

| Header | Value |
|---|---|
| `X-TokenPak-Deterministic` | `on` (`rejected` / `error` on fail-loud preflight responses) |
| `X-TokenPak-Deterministic-Fingerprint` | `sha256:<hex>` (see above) |
| `X-TokenPak-Deterministic-Provider` | resolved upstream provider (e.g. `anthropic`) |
| `X-TokenPak-Deterministic-Model` | model from the request body |
| `X-TokenPak-Deterministic-Seed` | only when the client supplied `seed` in the body |
| `X-TokenPak-Deterministic-Params` | compact JSON of generation-control fields present in the request (`temperature`, `top_p`, `top_k`, `stop`, `stop_sequences`, `max_tokens`) |
| `X-TokenPak-Deterministic-Fallback-Used` | `false` |
| `X-TokenPak-Deterministic-Retry-Used` | `false` |
| `X-TokenPak-Deterministic-Cache-Substitution-Used` | `false` |
| `X-TokenPak-Deterministic-Prompt-Mutation-Delta-Tokens` | estimated token delta between the TIP-stripped request and the forwarded bytes (0 unless a recorded transform ran) |
| `X-TokenPak-Deterministic-Adapter-Required-Transform` | comma-separated recorded transforms (`tip_header_strip`, plus `dlp_redact` when redaction fired); `none` when empty |

`model_version` is omitted: providers on this path do not expose a distinct
model-version surface beyond the request model string.

## Limitations (honest contour)

- Provider-side nondeterminism is out of scope: the proxy guarantees the
  *request* is reproducible and un-mutated, not that the provider samples
  deterministically. Do not assume Anthropic supports `seed`.
- The directive must lead the **first user message**; mid-conversation
  occurrences are content, not directives.
- If the spend guard layer is disabled, the proxy still strips the directive
  before forwarding (the directive text never reaches the provider).
