# TokenPak

[![PyPI version](https://img.shields.io/pypi/v/tokenpak.svg)](https://pypi.org/project/tokenpak/)
[![Python 3.10+](https://img.shields.io/pypi/pyversions/tokenpak.svg)](https://pypi.org/project/tokenpak/)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-green.svg)](LICENSE)
<!-- CI badge: pending repo transfer to tokenpak/tokenpak — add after transfer is confirmed -->

**Your agent starts every session not knowing what you already decided.**

So it asks again. Or it doesn't ask, and rebuilds something you ruled out last week. The
context exists — in your repo, your notes, your last session — but nothing puts it in front of
the model at the moment the model needs it.

TokenPak is a local proxy that closes that gap. On every eligible request, **before the model
is given the task**, it searches your indexed vault, fits the best matches to a token budget,
and splices them into the request. Then it tells you what it added and where each piece came
from.

No cloud. No credentials stored. If retrieval fails, your request goes through unchanged.

> **The open logistics layer for AI context.**

---

## Why this is different from a retrieval tool

Most integrations expose retrieval as a tool and wait for the model to call it. That leaves the
decision to a model that has usually already formed a plan — and a model that has formed a plan
does not know it was missing something.

| Model-invoked retrieval | TokenPak |
|---|---|
| The model decides whether to retrieve | The proxy evaluates every eligible request |
| Behaviour varies by model | The same rules apply regardless of model |
| Retrieval costs an extra agent turn | Context arrives with the first inference |
| Easy to skip, and invisible when skipped | The receipt shows what was included |

---

## What you get

### 1. The agent doesn't have to remember to look

Retrieval runs in the request path, not as a tool call. Requests are matched against your
indexed vault, ranked, fitted to a budget, and forwarded.

Eligibility is deliberate and inspectable: prompts below a size threshold, small fast models,
and weak matches are skipped rather than padded. See
[docs/configuration](docs/configuration/) for the thresholds and how to change them.

### 2. You see exactly what was added, and why

Every injection reports its sources and its token cost. This is the point — a component that
edits your prompts should be auditable, not trusted on faith.

```bash
tokenpak status --tip-cache   # what was reused vs re-sent
tokenpak savings              # measured on your own traffic
```

### 3. It doesn't change your bytes

On the Claude Code route, TokenPak splices at the byte level and never re-serialises your
JSON — because re-serialising changes how the request is billed. Byte-preservation is a
correctness requirement, not an optimisation.

### 4. Local, and reversible without loss

The proxy runs on `127.0.0.1`. Nothing leaves your machine except the request you were already
sending. Retrieval failures fail open. Removal is one command:

```bash
pip uninstall tokenpak
```

And what you built stays yours. Paks — the artifacts TokenPak packages context into — are plain
JSON files with a sha256 checksum, written to your disk. They open with the proxy stopped, and
they stay readable after TokenPak is uninstalled. Check for yourself:

```bash
tokenpak pak create ./docs -o project.pak.json   # package a directory
tokenpak stop                                    # or never start the proxy at all
cat project.pak.json                             # plain JSON, checksum included
```

Context that only your vendor can read is a lock-in mechanism wearing a memory costume. Yours
should still be there when the tool isn't.

---

## Try it without credentials

Inspect the compression stage offline, with no provider spend:

```bash
tokenpak demo
```

```
┌──────────────────────────────────────────────────────┐
│  TokenPak — Offline Fixture Demo (illustrative)      │
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

> This is a fixture, not a measurement of your workload. Token counts vary by route and
> workload. Measure your own with `tokenpak savings`.

---

## First measured receipt in three commands

Prerequisites: Python 3.10+ and an already authenticated supported client. The reference path
below uses Codex and reuses its existing OAuth login and default model. An API key or explicit
model override is optional, not required. Run it from a project with enough real history for an
eligible multi-turn request; real provider usage may count against your subscription or incur
provider charges.

```bash
python -m pip install tokenpak
tokenpak serve --profile aggressive --stats-footer  # terminal 1; leave running
tokenpak codex  # terminal 2; uses your existing login and default model
```

Make a normal context-bearing request, then continue that same topic. The first request may
correctly be ineligible because there is no historical context yet; the first eligible request
prints the measured before/after token receipt in terminal 1. The dollar figure is estimated
from TokenPak's model-pricing table. This session-only footer is off by default and does not
alter the provider response.

See the [first receipt guide](docs/first-receipt.md) for prerequisites, expected output, and
routes that are not eligible for compression savings.

---

## Works with

**First-class integrations** — Codex (`tokenpak codex`) · Claude Code (`tokenpak integrate claude-code`)

Both ship a dedicated launcher, credential reuse, a doctor command, and a proxy path built for
that client specifically.

**Tested SDK adapters** — OpenAI SDK · Anthropic SDK · LiteLLM

**Compatibility targets, not yet verified** — Cursor · Cline · Continue · Aider

These work by pointing the client's base URL at the proxy, but are not covered by tests.

Run `tokenpak integrate` for setup guides. Per-client status and coverage are tracked in
[docs/adapter-compatibility-matrix.md](docs/adapter-compatibility-matrix.md) — that file is the
ground truth, not this list.

---

## Install

```bash
pip install tokenpak
```

Retrieval needs an index before it can return anything. See
[docs/quickstart.md](docs/quickstart.md) for virtual-env setup, indexing, and per-client
configuration.

Requirements: Python 3.10+. No external dependencies for core functionality.

Exposing the proxy beyond `127.0.0.1`? Set `TOKENPAK_PROXY_AUTH_TOKEN` to a shared secret to
require `Authorization: Bearer <token>` on remote requests (see
[docs/configuration/proxy-auth.md](docs/configuration/proxy-auth.md)).

---

## Runnable examples

The PyPI wheel keeps the install slim and does not bundle the repository's top-level examples.
To run them after a normal package install, clone or download the source tree:

```bash
git clone https://github.com/tokenpak/tokenpak.git
cd tokenpak
python -m venv .venv
source .venv/bin/activate
python -m pip install -U tokenpak
python examples/basic_compression.py
```

`examples/basic_compression.py` is local-first and does not require provider credentials. See
[examples/README.md](examples/README.md) for the full examples index and the developer
editable-install path.

---

## What's included (Free)

> **Dispatch (v0.1-alpha preview):** turn a request into a scoped, resumable, reviewable
> workflow from the CLI. It is a source/`main`-branch preview and is not yet part of a released
> `pip install tokenpak`; see the [Dispatch guide](docs/guides/dispatch.md).

- **Pre-inference context injection** — indexed-vault retrieval in the request path, budgeted
  and source-attributed, on every eligible request
- **Context compression** — deterministic token reduction on real agent workloads. Savings are
  route-specific: direct API, CLI, and uncached repeated-agent loops are the best fit, while
  Claude Code/TUI routes may show lower incremental savings when the provider cache already
  handled repeated context. Measure your own with `tokenpak savings`; overhead is documented in
  [docs/LATENCY.md](docs/LATENCY.md)
- **Client integration** — one command wires the clients listed under *Works with*
- **Routing policy** — routing rules and fallback policy are configurable from the CLI as
  scaffolding; in-flight enforcement is not active by default
- **Cost tracking** — per model, per session, per agent; local SQLite, zero cloud
- **Spend Guard** — pre-send circuit breaker; blocks runaway requests before the provider call.
  Catches both single-request spikes and the death-by-1000-cuts pattern via session-cumulative
  tracking. See [docs/spend-guard.md](docs/spend-guard.md)
- **Vault indexing + search** — index your codebase; search without an LLM call
- **MultiPak Phase 1 OSS surface** — read-only Vault Pak adapter, companion journal
  promotion-candidate marking, `tokenpak pak` CLI, `/pak/v1/*` proxy stubs. Scored recall
  ranking, the capture pipeline, Handoff Paks, and anchor hydration require `tokenpak-paid`
  (Pro). See [docs/multipak.md](docs/multipak.md)
- **CLI + proxy server** — `tokenpak serve`, `tokenpak cost`, `tokenpak savings`
- **Value proof** — `tokenpak prove run` benchmarks direct API vs. TokenPak on your own prompt
  workload and prints a side-by-side cost/token report. See the
  [value proof guide](docs/guides/value-proof.md)
- **A/B testing and replay/debug** — compare compression configs, replay past requests
- **Built-in compression recipes** — YAML, customizable

---

## About cost

Sending less repeated context costs less. That follows from the mechanism — it isn't the reason
to run it, and it isn't a number we will quote at you. Route, workload, and provider caching all
move it. Measure yours with `tokenpak savings` and `tokenpak prove run`.

---

## Open source & editions

TokenPak's core is Apache-2.0 open source; TokenPak Pro and hosted services are proprietary.
Commercial packaging is not published yet.

---

## Support

- **Docs:** [docs/quickstart.md](docs/quickstart.md) · [API reference](docs/api-tpk-v1.md) · [examples/README.md](examples/README.md)
- **Issues:** [github.com/tokenpak/tokenpak/issues](https://github.com/tokenpak/tokenpak/issues)
- **Discussions:** [github.com/tokenpak/tokenpak/discussions](https://github.com/tokenpak/tokenpak/discussions)
- **Email:** hello@tokenpak.ai

---

## License

The TokenPak open-source core is licensed under the Apache License 2.0 — see [LICENSE](LICENSE).
TokenPak Pro and hosted services are proprietary.

### Trademark

"TokenPak", the TokenPak name, logo, and brand assets are trademarks of TokenPak and are
**not** licensed under Apache-2.0 (Apache-2.0 §6 grants no trademark rights). Nominative and
reference use — for example "works with TokenPak" or "a plugin for TokenPak" — is fine. Using
the name or logo in a way that implies endorsement, sponsorship, or affiliation, or naming a
fork, product, or service "TokenPak" (or something confusingly similar), is not.
