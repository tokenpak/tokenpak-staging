# TokenPak — Cut your LLM token spend — local first

[![PyPI version](https://img.shields.io/pypi/v/tokenpak.svg)](https://pypi.org/project/tokenpak/)
[![Python 3.10+](https://img.shields.io/pypi/pyversions/tokenpak.svg)](https://pypi.org/project/tokenpak/)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-green.svg)](LICENSE)
<!-- CI badge: pending repo transfer to tokenpak/tokenpak — add after transfer is confirmed -->

> **The open logistics layer for AI context.**

TokenPak starts as a local proxy that **packs AI requests** before they ship — reducing wasted context and recording receipts for what changed. Fewer tokens, lower cost. No code changes to your app, no TokenPak cloud dependency, and provider credentials stay in your local client/provider environment.

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

Then verify it's working:

```bash
tokenpak demo
```

```
┌──────────────────────────────────────────────────────┐
│  TokenPak — Live Compression Demo (illustrative)     │
├──────────────────────────────────────────────────────┤
│  Scenario              DevOps agent (config + logs)  │
│  Savings drivers                      dedup + alias  │
├──────────────────────────────────────────────────────┤
│  Original                                747 tokens  │
│  Compressed                              502 tokens  │
│  Fewer tokens                            245 tokens  │
├──────────────────────────────────────────────────────┤
│  Stages: dedup, alias, segmentize, directives        │
└──────────────────────────────────────────────────────┘
```

> Illustrative fixture — token counts vary by workload. Measure your own with `tokenpak savings`; receipt-backed ranges publish once the benchmark lane lands.

---

## Works with

Public setup guides cover **Claude Code**, **Cursor**, **Cline**, **Continue.dev**, **Aider**, **OpenAI SDK**, **Anthropic SDK**, **LiteLLM**, and **Codex**.

Run `tokenpak integrate` to see the client list, detected local tools, and which clients support automatic config writes.

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

## What's included (Free)

- **Context compression** — deterministic token reduction on real workloads. Measure your own savings with `tokenpak savings` (reproduce the headline benchmark with `make benchmark-headline`)
- **Client integration** — guided setup for local clients and SDKs; automatic config writes where supported
- **Local proxy routing** — run on `127.0.0.1`, forward to your configured provider, and keep advanced routing explicit
- **Cost tracking** — per model and per request/session where available; local SQLite, zero cloud
- **TIP Spend Guard** — pre-send circuit breaker; blocks runaway requests before provider call. Yes/No release or `[TIP: allow=once max=$X]` directive. Catches both single-request spikes and the death-by-1000-cuts pattern via session-cumulative tracking. See [docs/spend-guard.md](docs/spend-guard.md).
- **Vault indexing + semantic search** — index your codebase; search without an LLM call
- **PAK continuity surface** — read-only Vault Pak adapter, local `tokenpak pak create/inspect/export/import/status`, and `/pak/v1/*` proxy stubs. Ranked recall, capture pipelines, Handoff Paks, and anchor hydration are not part of the OSS Phase 0 path. See [docs/multipak.md](docs/multipak.md).
- **CLI + proxy server** — `tokenpak serve`, `tokenpak cost`, `tokenpak savings`
- **A/B testing and local diagnostics** — compare compression configs and inspect recent local request data
- **50 built-in compression recipes** — YAML, customizable

Repeated context is reused from cache instead of re-sent on every call. See [docs/quickstart.md](docs/quickstart.md) and [docs/api-tpk-v1.md](docs/api-tpk-v1.md) to get started.

---

## Open source & editions

TokenPak's core is Apache-2.0 open source; TokenPak Pro and hosted services are proprietary. Commercial packaging is not published yet.

---

## Support

- **Docs:** [docs/quickstart.md](docs/quickstart.md) · [API reference](docs/api-tpk-v1.md)
- **Issues:** [github.com/tokenpak/tokenpak/issues](https://github.com/tokenpak/tokenpak/issues)
- **Discussions:** [github.com/tokenpak/tokenpak/discussions](https://github.com/tokenpak/tokenpak/discussions)
- **Email:** hello@tokenpak.ai

---

## License

The TokenPak open-source core is licensed under the Apache License 2.0 — see [LICENSE](LICENSE). TokenPak Pro and hosted services are proprietary.

### Trademark

"TokenPak", the TokenPak name, logo, and brand assets are trademarks of TokenPak and are **not** licensed under Apache-2.0 (Apache-2.0 §6 grants no trademark rights). Nominative and reference use — for example "works with TokenPak" or "a plugin for TokenPak" — is fine. Using the name or logo in a way that implies endorsement, sponsorship, or affiliation, or naming a fork, product, or service "TokenPak" (or something confusingly similar), is not.
