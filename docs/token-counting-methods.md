# Token-counting method reconciliation

TokenPak counts tokens three different ways in different places, chosen for
different tradeoffs (accuracy vs. speed vs. no-network-dependency vs.
provider-agnosticism). This note names where each method is load-bearing so
the tradeoffs are visible in one place, rather than only inside each call
site's local comment. **This is a reference note, not a consolidation** — the
three methods stay independent; nothing here changes behavior.

## The three methods

| Method | What it measures | Typical error vs. real provider tokenization |
|---|---|---|
| `tiktoken` | Real BPE tokenization via the `tiktoken` library | Exact for OpenAI-family models; a close approximation for other providers (no official Anthropic/Google tokenizer is vendored) |
| chars ÷ 4 | `len(text) // 4` over a Python string | Commonly cited as within a few percent for English prose; degrades for code, non-English text, and dense JSON/markup |
| bytes ÷ 4 | `len(text.encode("utf-8")) // 4` (or a raw byte length already on hand) | Similar to chars/4 for ASCII text; more correct than chars/4 for multibyte (non-ASCII) text, since it counts encoded bytes rather than code points |

None of these is "the" token count — each caller picked the one that fits its
constraint (speed, no `tiktoken` dependency, no network, byte-length already
in hand, etc.). A caller that surfaces a count to a user should disclose
which method produced it (several already do — see below).

## Where `tiktoken` is load-bearing

`tiktoken` is used where an accurate count materially changes what gets sent
or kept, and the dependency cost is acceptable:

- **`tokenpak/telemetry/tokens.py`** — the shared `count_tokens()` /
  `truncate_to_tokens()` utility used across the transcript parser, vault
  index, SQLite backend, and MCP tools. Exposes `tokenizer_name()` so callers
  can disclose the active estimator.
- **`tokenpak/telemetry/budgeter.py`** — `Budgeter.allocate()` enforces a hard
  token budget across prompt-assembly buckets (state/recent/evidence/tools),
  trimming components that don't fit. An inaccurate count here would either
  overflow the real provider budget or trim more than necessary.
- **`tokenpak/compression/context_composer.py`**, **`span_extractor.py`**,
  **`pack.py`** — the compression pipeline's budget-fitting logic (which
  chunks/spans/blocks survive into the final packed prompt) is built on
  `tiktoken` counts, since these decisions directly shape the text sent
  onward to the provider.
- **`tokenpak/proxy/token_cache.py`** — an LRU-cached counter consumed by the
  request-forecast/spend-guard projection path.
- **`tokenpak/proxy/intelligence/server.py`** — backs the `/v1/compress` and
  `/v1/budget` endpoints' trim-to-budget loop.
- **`tokenpak/compression/evidence_pack.py`** — reporting/stats only (total
  tokens across evidence items).

Every one of the above falls back to chars ÷ 4 when `tiktoken` is not
installed (it's an optional dependency) — see next section.

## Where chars ÷ 4 is load-bearing

This is the dominant method by call-site count. Two flavors exist:

1. **The `tiktoken`-unavailable fallback** for every module listed above —
   same function, same call site, `except ImportError` branch.
2. **Standalone estimators**, used where a full tokenizer dependency isn't
   worth it for the decision being made: CLI command reports
   (`tokenpak/cli/commands/compress_cmd.py`, `optimize_prompt.py`, `pak.py`),
   SDK/integration adapters (`tokenpak/sdk/*`), vault retrieval/injection
   sizing (`tokenpak/vault/*`), most compression-engine estimators
   (`tokenpak/compression/*`), provider request-token approximations
   (`tokenpak/proxy/providers/{anthropic,openai,google}.py`), and Spend
   Guard's pre-flight cost projection (`tokenpak/proxy/spend_guard/estimator.py`,
   which defines the module's own `_CHARS_PER_TOKEN = 4` constant).

Two call sites are worth calling out because they gate something other than
a report:

- **`tokenpak/companion/hooks/pre_send.py`** — the pre-send hook's prompt-text
  branch deliberately avoids `tiktoken` to stay well under its latency
  budget; the resulting estimate feeds the pre-send allow/block decision.
- **`tokenpak/proxy/app_endpoints.py`**'s `/tokens/estimate` handler labels
  its fallback response `estimator: "chars-per-4-heuristic"` with an explicit
  user-facing note that the count is approximate — the disclosure pattern
  other estimators should follow when they surface a number externally.
- **`tokenpak/proxy/server.py`**'s `_estimate_tokens_from_body()` and
  **`tokenpak/proxy/server_async.py`**'s `_estimate_tokens()` — their primary
  parsed-JSON paths use chars ÷ 4. The synchronous estimator counts strings
  in recognized `messages`, `input`, or `contents` structures; the asynchronous
  estimator counts `messages` or `input` plus `system`. They use raw bytes ÷ 4
  only when JSON parsing or recognized-structure traversal fails.

There is no single shared constant for chars ÷ 4 across the codebase; each
module defines its own `4` (or a locally named `_CHARS_PER_TOKEN`). That is a
known inconsistency, not a defect this note is fixing — see "Out of scope"
below.

## Where bytes ÷ 4 is load-bearing

Rarer, and used specifically where a byte length is already on hand (a raw
request body, a file's on-disk size) and re-decoding to a string first would
be wasted work, or where multibyte correctness matters more than raw speed:

- **`tokenpak/telemetry/tokens.py`**'s exported `estimate_tokens()` — a
  public-API convenience function distinct from `count_tokens()`; counts
  UTF-8-encoded byte length rather than character count so multibyte text is
  handled correctly.
- **`tokenpak/companion/hooks/pre_send.py`** — the transcript-file branch
  uses `os.path.getsize(...) // 4` (the file is `stat`'d, never opened) for
  speed, alongside the chars ÷ 4 branch for inline prompt text in the same
  function.
- **`tokenpak/proxy/server.py`**'s `_estimate_tokens_from_body()` and
  **`tokenpak/proxy/server_async.py`**'s `_estimate_tokens()` — bytes ÷ 4 is
  their fallback when JSON parsing or recognized-structure traversal fails;
  their primary parsed-string paths are listed in the chars ÷ 4 section above.
- **`tokenpak/proxy/server.py`**'s `_stable_cache_hook` closure — proxy-side
  observe-only telemetry over the raw request body. The body forwarded to the
  provider is never altered by this estimate.
- **`tokenpak/proxy/capsule_integration.py`** — the outer fallback when the
  request body doesn't parse as JSON (the primary path there is a chars ÷ 4
  walk of the parsed JSON, promoted to bytes ÷ 4 only on parse failure).
- **`tokenpak/proxy/cache_invalidation_alerts.py`** — estimates tokens lost
  to a cache-prefix invalidation, feeding a lost-savings dollar figure shown
  to the user.
- **`tokenpak/cli/cli_diagnose.py`** — runs the ratio in reverse (tokens × 4
  → estimated bytes) purely to size a diagnostic cache-memory display; not a
  token count at all, included here for completeness since it uses the same
  4:1 constant.

## Out of scope for this note

- Consolidating the ~2 dozen independent chars ÷ 4 implementations behind one
  shared constant/helper. That's a real cleanup opportunity but a separate,
  larger change — this note only maps what exists today.
- Changing which method any call site uses. Every call site above keeps its
  existing method unchanged.
