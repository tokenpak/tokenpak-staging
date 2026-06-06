# TokenPak — Cut your LLM token spend — zero config

[![PyPI version](https://img.shields.io/pypi/v/tokenpak.svg)](https://pypi.org/project/tokenpak/)
[![Python 3.10+](https://img.shields.io/pypi/pyversions/tokenpak.svg)](https://pypi.org/project/tokenpak/)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-green.svg)](LICENSE)
![Status: Beta](https://img.shields.io/badge/status-beta-orange.svg)

TokenPak is a local-first proxy that applies **Prompt Packing** to your LLM context before it reaches the provider — fewer tokens, lower cost, same results. No code changes, no cloud, no credentials stored.

**Status:** Beta — APIs and CLI may change between releases.

---

## 30-second demo

```bash
pip install tokenpak
tokenpak serve                          # start proxy at localhost:8766
tokenpak integrate claude-code --apply  # wire Claude Code to the proxy
```

```
✅ Applied: Updated ~/.claude/settings.json (2 changes).
```

Then see it work on your own machine:

```bash
tokenpak demo
```

Run the local demo to inspect the Prompt Packing stages and a sample savings estimate on your machine.

---

## Works with

TokenPak speaks the OpenAI and Anthropic HTTP APIs, so any client that targets those endpoints can route through it. The table below reflects what is verified in the repo's [adapter compatibility matrix](docs/adapter-compatibility-matrix.md) versus what is not yet verified.

| Client / SDK | Status |
|--------------|--------|
| Claude Code | ✅ Supported — `tokenpak integrate claude-code` |
| OpenAI SDK | ✅ Supported — tested |
| Anthropic SDK | ✅ Supported — tested |
| LiteLLM | ✅ Supported — tested |
| Cursor | ⚠️ Experimental — verify before relying on it |
| Cline | ⚠️ Experimental — verify before relying on it |
| Continue.dev | ⚠️ Experimental — verify before relying on it |
| Aider | ⚠️ Experimental — verify before relying on it |
| Codex | ⚠️ Experimental — verify before relying on it |

Run `tokenpak integrate` to see the current client list with setup guides for each.

---

## Install

```bash
pip install tokenpak
```

See [docs/quickstart.md](docs/quickstart.md) for virtual-env setup and per-client configuration.

Requirements: Python 3.10+. No external dependencies for core functionality.

Exposing the proxy beyond `127.0.0.1`? Set `TOKENPAK_PROXY_AUTH_TOKEN` to a
shared secret to require `Authorization: Bearer <token>` on remote requests
(see [docs/configuration/proxy-auth.md](docs/configuration/proxy-auth.md)).

---

## What's included (OSS)

- **Prompt Packing** — fewer tokens on real agent workloads.
  Reproduce on your own workload: `make benchmark-headline`
- **Client integration** — one command wires Claude Code and other clients to the proxy
- **Model routing** — send requests to the right model automatically, with fallback rules
- **Cost tracking** — per model, per session, per agent; local SQLite, zero cloud
- **TIP Spend Guard** — pre-send circuit breaker; blocks runaway requests before provider call. Yes/No release or `[TIP: allow=once max=$X]` directive. Catches both single-request spikes and the death-by-1000-cuts pattern via session-cumulative tracking. See [docs/spend-guard.md](docs/spend-guard.md).
- **Vault indexing + semantic search** — index your codebase; search without an LLM call
- **MultiPak (OSS surface)** — read-only Vault Pak adapter, companion journal promotion-candidate marking, `tokenpak pak` CLI, `/pak/v1/*` proxy stubs. See [docs/multipak.md](docs/multipak.md).
- **CLI + proxy server** — `tokenpak serve`, `tokenpak cost`, `tokenpak savings`
- **A/B testing and replay/debug** — compare Prompt Packing configs, replay past requests
- **50 built-in compression recipes** — YAML, customizable

In favorable repeated-context workloads, cache hits can reach up to 90%+. See [docs/quickstart.md](docs/quickstart.md) and [docs/api-tpk-v1.md](docs/api-tpk-v1.md) to get started.

---

## Open source & editions

TokenPak's core is Apache-2.0 open source; TokenPak Pro and hosted services are proprietary. Commercial packaging is not published yet.

---

## Support

- **Docs:** [docs/quickstart.md](docs/quickstart.md) · [API reference](docs/api-tpk-v1.md)
- **Issues:** [github.com/tokenpak/tokenpak/issues](https://github.com/tokenpak/tokenpak/issues)
- **Discussions:** [github.com/tokenpak/tokenpak/discussions](https://github.com/tokenpak/tokenpak/discussions)
- **Email:** hello@tokenpak.ai
