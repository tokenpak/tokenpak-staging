# TokenPak byte-fidelity benchmark harness

This directory holds the canonical provider-integration harness used by Initiative 1 (services-stage-logic) to byte-fidelity-gate each stage migration in `services/*_service/` against live provider traffic.

**Authorization model:** OAuth subscriptions (Claude Code CLI + OpenAI Codex CLI on Sue's machine). No raw API keys required; the proxy forwards OAuth tokens transparently per the existing `proxy/oauth.py` module.

## Why it exists

The services-stage-logic initiative (`2026-04-20-tokenpak-services-pipeline-stage-logic`) migrates real compression / cache / routing / telemetry / security logic out of `proxy/server.py` into the matching `services/*_service/` stage. Each stage migration MUST preserve byte-fidelity on the passthrough path (Constitution §5.2 Condition A) — outbound request bytes + inbound response bytes bit-identical pre and post.

Unit tests stay green regardless of whether `proxy/server.py` actually routes through `services.execute` — they don't exercise the full provider round-trip. This harness closes that gap.

## What's in this directory

- `README.md` (this file) — the what / why
- `harness.py` — driver: starts `tokenpak serve`, runs scenarios, captures byte streams
- `scenarios/` — one directory per scenario; each holds the request payload + expected-shape metadata
- `baselines/` — captured `{request.bin, response.bin, headers.json}` per scenario (written on first run; checked against on subsequent runs)

## Running

Prerequisites on the host:
1. Claude Code CLI installed + authenticated via OAuth — verify `claude auth status`
2. OpenAI Codex CLI installed + authenticated via OAuth — verify `codex auth status`
3. Recent `tokenpak` (use `pip install -e .` from repo root or `pip install tokenpak==1.1.0`)

```bash
# Baseline capture — store reference bytes for each scenario
python3 examples/benchmarks/harness.py --capture-baseline

# Stage-migration verification — re-run all scenarios; byte-diff against baseline
python3 examples/benchmarks/harness.py --verify
```

## Scenario set

| ID | Name | Provider | Purpose |
|----|------|----------|---------|
| 01 | simple-anthropic | Claude (OAuth) | Minimal request; exercises byte-preserved passthrough |
| 02 | simple-openai | Codex (OAuth) | OpenAI equivalent of 01 |
| 03 | streaming-anthropic | Claude (OAuth) | SSE streaming; stages cannot mis-order chunks |
| 04 | cache-hit | Claude (OAuth) | Same request twice; second serves from proxy cache with `cache_origin=proxy` |
| 05 | compression-eligible | Claude (OAuth) | Large context; exercises compression pipeline |

## Stage-migration gate protocol

Each stage packet (S-PS-01 through S-PS-05) runs:

1. **Capture baseline** against pre-migration HEAD (if not already captured)
2. **Apply stage migration** — move the relevant logic from `proxy/server.py` into the matching `services/*_service/` stage
3. **Run `--verify`** — every scenario's outbound + inbound bytes + `X-TokenPak-*` headers MUST match baseline
4. If any scenario drifts: **revert the stage commit**; investigate; retry.

## OAuth specifics

The proxy's existing `proxy/oauth.py` handles OAuth token forwarding for both Anthropic and OpenAI. The key invariant: the proxy re-sends the client's OAuth `Authorization` header verbatim to the provider (after any compression-side modifications), preserving the token format the provider expects.

Do NOT hardcode API keys in this harness. If `claude auth status` or `codex auth status` reports logged out, re-authenticate with the provider CLI first; the harness picks up the session automatically via the CLI's normal transport path.

## Known limitations

- **Live provider cost:** each scenario hits a real provider endpoint. Small scenarios (~50 tokens in, ~100 tokens out) keep the cost minimal; avoid large-prompt scenarios unless you're testing compression.
- **Non-determinism:** streaming chunking can vary per provider load; the harness captures + compares **event stream structure**, not wall-clock timing. If a scenario shows transient drift, re-run; if persistent, investigate.
- **Credential expiry:** if OAuth refresh fails mid-scenario, the harness emits a clear error; re-authenticate + re-run.

## Decision records

- DECISION-MTC-P-AP-01 — live-traffic byte-fidelity baseline was infeasible in Phase A (no harness existed). This harness is the Phase A-retrospective + Initiative-1 pre-requisite.
- Kevin 2026-04-21: "Use Sue's machine to serve. API key should not be required, i will be using oauth subscription tied to anthropic and codex."
