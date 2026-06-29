# TokenPak Adapter Capability Matrix

**Last updated:** 2026-06-29 — `feat/provider-adapter-certification-matrix-2026-06-25`

This document states **what TokenPak can prove per provider** based on code-level analysis and
offline smoke tests. It supersedes the SDK-version compatibility table that was here previously
(that table covered _which SDK versions work_; this matrix covers _which capabilities work_).

---

## User-Facing Target Labels

These labels match the runtime output from `tokenpak integrate`.

| Label | Meaning |
|-------|---------|
| **Supported** | TokenPak presents this as a regular Beta setup path. |
| **Tested** | Current repo tests or offline smoke proof cover the target behavior. |
| **Experimental** | TokenPak exposes setup help, but testers should verify before relying on it. |
| **Untested** | No current runnable proof is recorded for the target. Do not describe it as tested. |
| **Apply** | `tokenpak integrate <target> --apply` writes a supported config surface. |
| **Print-only** | `tokenpak integrate <target>` prints setup instructions; no config file is written. |

| Target | Runtime setup mode | Compatibility label | Current proof |
|--------|--------------------|---------------------|---------------|
| Claude Code | Apply | Supported; tested | CLI integration tests and apply/verify hooks |
| OpenAI SDK | Print-only | Supported; tested | SDK adapter round-trip tests |
| Anthropic SDK | Print-only | Supported; tested | SDK adapter round-trip tests |
| LiteLLM | Print-only | Supported; tested | LiteLLM adapter/unit/import-guard tests |
| Cursor | Apply | Experimental; untested | Apply path exists; no current certified host proof |
| Cline | Print-only | Experimental; untested | Manual setup only; no current certified host proof |
| Continue.dev | Apply | Experimental; untested | Apply path exists; no current certified host proof |
| Aider | Apply | Experimental; untested | Apply path exists; no current certified host proof |
| Codex | Print-only | Experimental; untested | Base setup is print-only; tier flags are separate |

## Capability Status Legend

| Symbol | Meaning |
|--------|---------|
| ✅ **tested** | Implemented, tested offline, behaves as documented |
| ⚠️ **partial** | Implemented but with known caveats or untested edge cases |
| ❌ **missing** | Not implemented or deliberately disabled |
| ❓ **untested** | Provider API does not document / expose this feature; cannot prove either way without live credentials |

> **Live-only gaps** — any cell marked ❓ requires a live provider call to verify.
> These are reported explicitly so they are not silently presented as green.
> `live_api_allowed: false` means they stay ❓ until a certified live-call task is dispatched.

---

## Proxy Adapter Registry

Adapters registered in `build_default_registry()` (`tokenpak/proxy/adapters/__init__.py`),
listed highest-priority first:

| Priority | Source format | Class | Detection signals |
|----------|--------------|-------|-------------------|
| 300 | `anthropic-messages` | `AnthropicAdapter` | path `/v1/messages`, `x-api-key` header, `anthropic-version` header |
| 270 | `openai-codex-responses` | `OpenAICodexResponsesAdapter` | path `/codex/responses`, `/v1/codex/responses`, or `/v1/responses` with JWT bearer |
| 260 | `openai-responses` | `OpenAIResponsesAdapter` | path `/v1/responses` |
| 255 | `xai-grok` | `GrokAdapter` | `api.x.ai` host, `x-xai-api-key` header, or body model starts with `grok-` |
| 250 | `openai-chat` | `OpenAIChatAdapter` | path `/v1/chat/completions` |
| 240 | `google-generative-ai` | `GoogleGenerativeAIAdapter` | path `/v1beta/`, `x-goog-api-key` header, or `key=` in path |
| 0 | `passthrough` | `PassthroughAdapter` | catch-all (always matches) |

## Telemetry Adapter Registry

Adapters in `AdapterRegistry.build_default()` (`tokenpak/telemetry/adapters/registry.py`):

| Provider | Class | Detection key |
|----------|-------|---------------|
| `anthropic` | `AnthropicAdapter` | `stop_reason`, `type: "message"`, `anthropic-version` |
| `openai` | `OpenAIAdapter` | `choices` (Chat), `output` list + `object: "response"` (Responses API) |
| `gemini` | `GeminiAdapter` | `candidates`, `usageMetadata` |
| _(fallback)_ | `UnknownAdapter` | anything below 0.5 confidence |

---

## Capability Matrix — Proxy Adapters

**Columns:**
- **Token count** — accurate extraction of input/output tokens from response bodies
- **Streaming** — SSE streaming with token extraction from stream events
- **Tools** — round-trip fidelity of tool/function definitions through normalize/denormalize
- **Spend guard** — tokens feed into cost/spend enforcement reliably
- **Receipt** — produces `CanonicalUsage(usage_source=PROVIDER_REPORTED, confidence=HIGH)`
- **Recall inject** — system-prompt injection for vault recall (stable/volatile two-layer design)
- **Cache tokens** — cache read/write token counts extracted (used for receipt accuracy)

| Provider | Token count | Streaming | Tools | Spend guard | Receipt | Recall inject | Cache tokens |
|----------|------------|-----------|-------|-------------|---------|---------------|-------------|
| **Anthropic** | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ cacheable-injection | ✅ read+write |
| **OpenAI Chat** | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ generic | ⚠️ read only |
| **OpenAI Responses** | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ generic | ⚠️ read only |
| **OpenAI Codex** | ✅ | ✅ forced | ✅ | ✅ | ✅ | ✅ generic | ⚠️ read only |
| **Google / Gemini** | ⚠️ body only | ⚠️ ndjson | ✅ with translation | ⚠️ body only | ⚠️ when metadata present | ✅ generic | ⚠️ read only |
| **xAI Grok** | ✅ | ✅ | ✅ | ✅ cost model | ⚠️ via OpenAI adapter | ✅ generic | ❓ unknown |
| **Passthrough** | ❌ | ⚠️ generic | ⚠️ verbatim | ❌ | ❌ | ❌ noop | ❌ |

---

## Capability Detail per Provider

### Anthropic

**Source format:** `anthropic-messages` | **Default upstream:** `https://api.anthropic.com`

| Capability | Status | Evidence |
|-----------|--------|---------|
| Token count | ✅ | `usage.input_tokens` / `output_tokens` always in response; telemetry adapter: `confidence=HIGH` |
| Streaming | ✅ | SSE format `anthropic-sse`; `_extract_sse_output_tokens` reads `message_delta.usage.output_tokens` |
| Tools | ✅ | `tools` field round-trips; `tool_use` stop_reason mapped in telemetry |
| Spend guard | ✅ | Provider-reported tokens with HIGH confidence feed budget enforcement |
| Receipt | ✅ | `CanonicalUsage(usage_source=PROVIDER_REPORTED, confidence=HIGH)` always |
| Recall inject | ✅ | Full two-layer cacheable-injection: stable prefix gets `cache_control: ephemeral`; volatile block does NOT; TTL bypass when client already manages cache_control |
| Cache tokens | ✅ | `cache_read_input_tokens` (read) + `cache_creation_input_tokens` (write) both extracted |

**Caveats:**
- `_body_has_explicit_ttl` check: when the client sends explicit TTL cache_control, TokenPak does not add its own markers (`cache_origin="client"`). Cali's cacheable injection is attribution-aware.
- Streaming token count arrives in the final `message_delta` event; intermediate chunks have 0.

---

### OpenAI Chat Completions

**Source format:** `openai-chat` | **Default upstream:** `https://api.openai.com`

| Capability | Status | Evidence |
|-----------|--------|---------|
| Token count | ✅ | `usage.prompt_tokens` / `completion_tokens`; base class `extract_response_tokens` |
| Streaming | ✅ | SSE format `openai-sse`; token count in final SSE event `usage.completion_tokens` |
| Tools | ✅ | `tools` + legacy `functions` → promoted to `tools`; `tool_choice` / `parallel_tool_calls` in generation params |
| Spend guard | ✅ | Provider-reported tokens with HIGH confidence |
| Receipt | ✅ | `CanonicalUsage(usage_source=PROVIDER_REPORTED, confidence=HIGH)` when `usage` present |
| Recall inject | ✅ | Generic two-layer injection via base class; `cache_origin="unknown"` (OpenAI does not expose proxy-attributable cache placement) |
| Cache tokens | ⚠️ | Cache read: `usage.prompt_tokens_details.cached_tokens` — extracted. Cache write: **not exposed by OpenAI API**; always 0. |

**Caveats:**
- Legacy `functions` key is auto-promoted to `tools` format on normalize; transparent to callers.
- `cache_write` is always 0; no way to know how many tokens OpenAI wrote to its prefix cache.

---

### OpenAI Responses API

**Source format:** `openai-responses` | **Default upstream:** `https://api.openai.com`

| Capability | Status | Evidence |
|-----------|--------|---------|
| Token count | ✅ | Same usage schema as Chat Completions |
| Streaming | ✅ | SSE format `openai-responses-sse` |
| Tools | ✅ | Tools sorted deterministically for cache-key stability |
| Spend guard | ✅ | Same as Chat Completions |
| Receipt | ✅ | Same as Chat Completions |
| Recall inject | ✅ | Generic injection via base class |
| Cache tokens | ⚠️ | Cache read only (same as Chat); cache_write=0 |

**Caveats:**
- `input` field supports four shapes: string, content array, message array, single message. All round-trip correctly via `_input_format` tag in `raw_extra`.
- `prompt_cache_key` is auto-computed from a stable sha256 of model + instructions + tools + message prefix. Ensures consistent caching across turns.
- Capability label `tip.cache.semantic.v1` declared (TIP-04 eligible for semantic cache stage).

---

### OpenAI Codex Responses

**Source format:** `openai-codex-responses` | **Default upstream:** `https://chatgpt.com/backend-api`

| Capability | Status | Evidence |
|-----------|--------|---------|
| Token count | ✅ | Inherits from `OpenAIResponsesAdapter` |
| Streaming | ✅ | **Always forced to `stream=True`** — ChatGPT backend requires SSE; `store=False` always |
| Tools | ✅ | Inherits from `OpenAIResponsesAdapter` |
| Spend guard | ✅ | Inherits token extraction |
| Receipt | ✅ | Inherits from OpenAI telemetry adapter |
| Recall inject | ✅ | Inherits generic injection |
| Cache tokens | ⚠️ | Cache read only (inherits OpenAI); cache_write=0 |

**Caveats:**
- Requires `curl_cffi` for Cloudflare bypass on `chatgpt.com`; falls back to urllib3 (likely 403).
- Detection: JWT bearer on `/v1/responses` (starts with `eyJ`) routes to Codex; `sk-` API keys route to standard OpenAI Responses.
- `max_output_tokens` is stripped from payload (unsupported by ChatGPT backend).
- `codex_responses_payload_fixup()` can be applied without a full normalize/denormalize round-trip.

---

### Google Generative AI (Gemini)

**Source format:** `google-generative-ai` | **Default upstream:** `https://generativelanguage.googleapis.com`

| Capability | Status | Evidence |
|-----------|--------|---------|
| Token count | ⚠️ | `usageMetadata.candidatesTokenCount` (output), `promptTokenCount` (input) — **only in non-streaming responses**; `usageMetadata` absent in streaming NDJSON chunks |
| Streaming | ⚠️ | NDJSON format (`google-ndjson`); streaming detected from URL (`streamGenerateContent`/`alt=sse`), not body; token count unreliable per-chunk |
| Tools | ✅ | Full translation: OpenAI `tools[]`, Anthropic `input_schema`, or Google `functionDeclarations` → Google native format; JSON Schema sanitization (removes `$schema`, `additionalProperties`, etc.); type name uppercasing |
| Spend guard | ⚠️ | Token counts available from non-streaming responses; streaming gaps mean spend guard may undercount until final response |
| Receipt | ⚠️ | `CanonicalUsage(confidence=HIGH)` when `usageMetadata` present; `confidence=LOW, usage_source=UNKNOWN` when absent (streaming chunks) |
| Recall inject | ✅ | Generic two-layer injection via base class; `cache_origin="unknown"` |
| Cache tokens | ⚠️ | `usageMetadata.cachedContentTokenCount` → `cache_read`; cache_write **not exposed** by Gemini API; always 0 |

**Caveats:**
- Google `functionDeclarations` format does not support: `$schema`, `$ref`, `$defs`, `additionalProperties`, `patternProperties`, `oneOf`, `anyOf`, `allOf`, `not`, `if/then/else`, `examples`, `default`, `title`, `format`. These are silently dropped on translation.
- Null types in JSON Schema are converted: `["string", "null"]` → `type: STRING, nullable: True`.
- Streaming token count gap: if spend guard relies on per-chunk accumulation, Google NDJSON may not emit `usageMetadata` until the final chunk. Partial spend tracking until chunk terminal.
- `systemInstruction` translated to/from `system` canonical field; `generationConfig` preserved verbatim.

---

### xAI Grok

**Source format:** `xai-grok` | **Default upstream:** `https://api.x.ai`

| Capability | Status | Evidence |
|-----------|--------|---------|
| Token count | ✅ | `usage.completion_tokens` for output; OpenAI-compatible schema |
| Streaming | ✅ | SSE format `openai-sse`; inherits OpenAI SSE parsing |
| Tools | ✅ | OpenAI-compatible tool format; `functions` legacy key promoted |
| Spend guard | ✅ | Built-in `estimate_cost()` with per-model pricing table (grok-3, grok-3-fast, grok-3-mini, grok-2 variants); USD per 1M tokens |
| Receipt | ⚠️ | No dedicated telemetry adapter in `build_default()`; OpenAI adapter detects via `choices` key (Grok uses OpenAI-compatible response shape). Works in practice but not explicitly tested. |
| Recall inject | ✅ | Generic two-layer injection via base class |
| Cache tokens | ❓ | xAI does not document cache token fields; no `prompt_tokens_details` equivalent observed; **live call required to verify** |

**Caveats:**
- Grok uses the OpenAI Chat Completions wire format. The xAI-specific detection (host/header/model prefix) fires before `OpenAIChatAdapter` (priority 255 > 250).
- `estimate_cost()` uses hardcoded pricing as of 2025-Q1. Prices may drift; not guaranteed accurate.
- Cache token gap: if xAI adds a `prompt_tokens_details.cached_tokens` field in the future, it will be picked up automatically by the OpenAI telemetry adapter. Until confirmed: ❓.

---

### Passthrough

**Source format:** `passthrough` | **Default upstream:** `https://api.anthropic.com` (never used for passthrough traffic)

| Capability | Status | Evidence |
|-----------|--------|---------|
| Token count | ❌ | No provider-specific extraction; `UnknownAdapter` fallback returns `proxy_estimate, LOW` |
| Streaming | ⚠️ | SSE format `generic`; bytes are forwarded unchanged |
| Tools | ⚠️ | `tools` preserved verbatim; no translation between formats |
| Spend guard | ❌ | No reliable token counts; spend guard cannot enforce limits |
| Receipt | ❌ | `CanonicalUsage(usage_source=PROXY_ESTIMATE, confidence=LOW)` from `UnknownAdapter` |
| Recall inject | ❌ | Deliberate NO-OP: `inject_system_context` returns body unchanged; `cache_origin="client"` trace emitted to avoid false attribution |
| Cache tokens | ❌ | Not extracted |

**Caveats:**
- Passthrough is intentionally byte-preserving. It is the correct choice for traffic where TokenPak should not touch the payload (custom provider formats, client-managed cache, non-JSON bodies).
- Any passthrough traffic will register 0 tokens in spend guard. This is by design, not a bug.

---

## Offline Smoke Commands

These commands validate the main adapters without live credentials.
Run from the repository root.

```bash
# Current offline proxy adapter + runtime-label proof
python -m pytest tests/test_proxy_adapters.py tests/cli/test_integrate_print_only_targets.py

# Legacy SDK adapter smoke, when the legacy tokenpak.adapters import surface is available
python -m pytest tests/test_adapter_roundtrip.py

# Lint
python -m ruff check tokenpak/cli/commands/integrate.py tests/cli/test_integrate_print_only_targets.py
```

Expected: current proxy adapter and runtime-label tests pass, no ruff errors.
`tests/test_adapter_roundtrip.py` may skip in slim checkouts that do not expose
the legacy `tokenpak.adapters` import surface; do not count a skip as live proof.

---

## Known Provider Gaps Needing Follow-Up

| Gap | Provider | Requires | Status |
|-----|----------|----------|--------|
| Cache write tokens | OpenAI (all) | OpenAI API exposes write count | ❌ missing — API limitation |
| Cache write tokens | Gemini | Gemini API exposes write count | ❌ missing — API limitation |
| Cache tokens | xAI Grok | Live call to verify `prompt_tokens_details` presence | ❓ blocked (live API) |
| Streaming token count | Gemini | Verify `usageMetadata` in final NDJSON chunk | ❓ blocked (live API) |
| Grok telemetry adapter | xAI | Add dedicated `GrokAdapter` to `build_default()` | ⚠️ partial — falls to OpenAI adapter |
| curl_cffi availability | OpenAI Codex | Verify installed in runtime venv | ⚠️ runtime check required |

---

## SDK Version Compatibility (legacy table)

The previous version of this file contained an SDK version compatibility matrix
(`TokenPak v1.0 × OpenAI SDK 1.x–2.x × Python 3.10–3.13` etc.). That data is still
accurate and is preserved in `docs/adapters/ARCHITECTURE.md`. The focus of this file
is now **capability proof per provider**, not SDK version ranges.

---

## Sources

- `tokenpak/proxy/adapters/__init__.py` — `build_default_registry()` priority table
- `tokenpak/cli/commands/integrate.py` — runtime target labels and apply/print-only modes
- `tokenpak/proxy/adapters/{anthropic,openai_chat,openai_responses,openai_codex_responses,google,grok,passthrough}_adapter.py`
- `tokenpak/telemetry/adapters/{anthropic,openai,gemini}.py` + `registry.py`
- `tokenpak/telemetry/canonical.py` — `CanonicalUsage`, `UsageSource`, `Confidence`
- `tests/test_proxy_adapters.py`, `tests/cli/test_integrate_print_only_targets.py`
- `tests/test_adapter_roundtrip.py` — legacy SDK smoke when its import gate is available
- Offline analysis only; `live_api_allowed: false`
