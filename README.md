# TokenPak — Cut your LLM token spend — local first

[![PyPI version](https://img.shields.io/pypi/v/tokenpak.svg)](https://pypi.org/project/tokenpak/)
[![Python 3.10+](https://img.shields.io/pypi/pyversions/tokenpak.svg)](https://pypi.org/project/tokenpak/)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-green.svg)](LICENSE)
![Status: Beta](https://img.shields.io/badge/status-beta-orange.svg)

> **The open logistics layer for AI context.**

TokenPak starts as a local proxy that **packs AI requests** before they ship — reducing wasted context and giving teams receipts for what changed. Fewer tokens, lower cost. No code changes, no TokenPak cloud dependency, and provider credentials stay in your local client/provider environment.

**Status:** Beta — APIs and CLI may change between releases.

---

## First measured receipt

```bash
pip install tokenpak
tokenpak start
curl -sS http://localhost:8766/v1/messages \
  -H "x-api-key: $ANTHROPIC_API_KEY" \
  -H "anthropic-version: 2023-06-01" \
  -H "content-type: application/json" \
  -d '{"model":"claude-3-5-sonnet-20241022","max_tokens":64,"messages":[{"role":"user","content":"Summarize this recurring project context and keep the answer short."}]}'
```

That third command sends one eligible provider request through the local proxy.
The durable local receipt is written to `~/.tpk/monitor.db` in the `requests`
table, with model, token, cost, cache-origin, and attribution fields. Inspect the
rollup with:

```bash
tokenpak status --json
```

Receipt values are measured from local proxy telemetry and provider usage data.
If a request has no attributable compression or cache savings, TokenPak reports
zero instead of inventing a number. See [docs/first-receipt.md](docs/first-receipt.md)
for the evidence boundary and deterministic regression fixture.

---

## Works with

TokenPak speaks the OpenAI and Anthropic HTTP APIs, so any client that targets those endpoints can route through it. The table below reflects what is verified in the repo's [adapter compatibility matrix](docs/adapter-compatibility-matrix.md) versus what is not yet verified.

| Client / SDK | Runtime setup mode | Compatibility label |
|--------------|--------------------|---------------------|
| Claude Code | `--apply` supported | Supported; tested |
| OpenAI SDK | Print-only snippet | Supported; tested |
| Anthropic SDK | Print-only snippet | Supported; tested |
| LiteLLM | Print-only snippet | Supported; tested |
| Cursor | `--apply` available | Experimental; untested |
| Cline | Print-only snippet | Experimental; untested |
| Continue.dev | `--apply` available | Experimental; untested |
| Aider | `--apply` available | Experimental; untested |
| Codex | Print-only base setup | Experimental; untested |

Run `tokenpak integrate` to see the current client list with setup guides for each.

---

## Install

```bash
pip install tokenpak
```

To upgrade an existing install (a plain `pip install tokenpak` will **not**
upgrade an already-installed version):

```bash
pip install -U tokenpak
# or
tokenpak update
```

### Optional features (extras)

The slim install keeps core functionality under 200 MB. Heavy optional features are opt-in:

| Extra | What it adds | Install |
|---|---|---|
| `retrieval` | Semantic/vector search (sentence-transformers, ~5 GB with torch) | `pip install tokenpak[retrieval]` |
| `code-compression` | AST-based code compression (tree-sitter-languages) | `pip install tokenpak[code-compression]` |
| `intelligence` | A/B optimizer analytics (scipy) | `pip install tokenpak[intelligence]` |
| `data` | Data analysis features (pandas) | `pip install tokenpak[data]` |
| `compression` | LLM-based compression engine (llmlingua) | `pip install tokenpak[compression]` |
| `integrations-litellm` | LiteLLM router middleware (litellm) | `pip install tokenpak[integrations-litellm]` |
| `dispatch` | Dispatch orchestration subsystem (pydantic, jsonschema) | `pip install tokenpak[dispatch]` |
| `full` | All of the above — restores legacy bundled behavior | `pip install tokenpak[full]` |

**Upgrading from v1.8.x or earlier?** Six heavy packages (`sentence-transformers`, `tree-sitter-languages`, `scipy`, `pandas`, `llmlingua`, `litellm`) moved from core to optional extras in v1.9.0. Install the extras you need above, or use `pip install tokenpak[full]` to restore the previous bundled behavior.

See [docs/quickstart.md](docs/quickstart.md) for virtual-env setup and per-client configuration.

Requirements: Python 3.10+. No external dependencies for core functionality.

Exposing the proxy beyond `127.0.0.1`? Set `TOKENPAK_PROXY_AUTH_TOKEN` to a
shared secret to require `Authorization: Bearer <token>` on remote requests
(see [docs/configuration/proxy-auth.md](docs/configuration/proxy-auth.md)).

---

## What's included (OSS)

> **Dispatch (v0.1-alpha preview):** turn a request into a scoped, resumable, reviewable workflow from the CLI. It is a source/`main`-branch preview and is not yet part of a released `pip install tokenpak`; see the [Dispatch guide](docs/guides/dispatch.md).

- **Prompt Packing** — fewer tokens on real agent workloads. Savings are
  route-specific: direct API, CLI, and uncached repeated-agent loops are the
  best fit, while Claude Code/TUI routes may show lower incremental savings
  when the provider cache already handled repeated context. Reproduce on your
  own workload with `make benchmark-headline`; inspect attribution with
  `tokenpak status --tip-cache`.
- **Client integration** — one command wires Claude Code and other clients to the proxy
- **Model routing** — send requests to the right model automatically, with fallback rules
- **Cost tracking** — per model, per session, per agent; local SQLite, zero cloud
- **TIP Spend Guard** — pre-send circuit breaker; blocks runaway requests before provider call. Yes/No release or `[TIP: allow=once max=$X]` directive. Catches both single-request spikes and the death-by-1000-cuts pattern via session-cumulative tracking. See [docs/spend-guard.md](docs/spend-guard.md).
- **Vault indexing + semantic search** — index your codebase; search without an LLM call
- **MultiPak (OSS surface)** — read-only Vault Pak adapter, companion journal promotion-candidate marking, `tokenpak pak` CLI, `/pak/v1/*` proxy stubs. See [docs/multipak.md](docs/multipak.md).
- **CLI + proxy server** — `tokenpak serve`, `tokenpak cost`, `tokenpak savings`
- **A/B testing and replay/debug** — compare Prompt Packing configs, replay past requests
- **50 built-in compression recipes** — YAML, customizable

Repeated context is reused from cache instead of re-sent on every call. See [docs/quickstart.md](docs/quickstart.md) and [docs/api-tpk-v1.md](docs/api-tpk-v1.md) to get started.

---

## Open source & editions

TokenPak's core is Apache-2.0 open source; TokenPak Pro and hosted services are proprietary. Commercial packaging is not published yet.

---

## License

The TokenPak open-source core is licensed under the Apache License 2.0 — see [LICENSE](LICENSE). TokenPak Pro and hosted services are proprietary.

### Trademark

"TokenPak", the TokenPak name, logo, and brand assets are trademarks of TokenPak and are **not** licensed under Apache-2.0 (Apache-2.0 §6 grants no trademark rights). Nominative and reference use — for example "works with TokenPak" or "a plugin for TokenPak" — is fine. Using the name or logo in a way that implies endorsement, sponsorship, or affiliation, or naming a fork, product, or service "TokenPak" (or something confusingly similar), is not.

---

## Support

- **Docs:** [docs/quickstart.md](docs/quickstart.md) · [API reference](docs/api-tpk-v1.md)
- **Issues:** [github.com/tokenpak/tokenpak/issues](https://github.com/tokenpak/tokenpak/issues)
- **Discussions:** [github.com/tokenpak/tokenpak/discussions](https://github.com/tokenpak/tokenpak/discussions)
- **Email:** hello@tokenpak.ai
