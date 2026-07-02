# Value Proof — Measure TokenPak's impact

`tokenpak prove` runs the same multi-turn scenario through two execution paths — direct API and tokenpak — and generates a side-by-side comparison report. Use it to measure actual token reduction, cost savings, and latency impact on your own workloads.

---

## Why run a proof?

The built-in `tokenpak savings` command shows accumulated savings from live traffic. A value proof answers a different question: **given a specific prompt workload, how much cheaper is tokenpak vs. direct API, right now, before you commit to wiring it up?**

Proofs are also useful after changes — if you tune compression settings, run the same scenario before and after to see the diff.

---

## Quick start

```bash
# Run the built-in default scenario (2-turn REST vs GraphQL question)
tokenpak prove run

# List all available scenarios
tokenpak prove list

# Run a named scenario
tokenpak prove run my-scenario

# Override the model
tokenpak prove run default --model gpt-4o

# Show a past proof result
tokenpak prove show prf_a1b2c3d4

# Create a new scenario interactively
tokenpak prove create --name my-scenario

# List registered providers and models
tokenpak prove providers
```

---

## Reading the report

```
  TokenPak Value Proof — "default"
  ======================================================================
  Arms: 2  |  Turns: 2  |  Proof: prf_3f7a1c2e

                             direct-api      tokenpak        vs [1]
  ----------------------------------------------------------------------
         platform/provider   direct/anth  tokenpak/anth
                     model   claude-son…   claude-son…

               Input tokens       15,482        8,143        -47.4%
         Cache-read tokens             0        7,203
              Output tokens         1,847        1,847          0.0%
                Total cost       $0.0511       $0.0338        -33.8%
                Total time         12.3s         11.1s         -9.8%
  ----------------------------------------------------------------------

  Per-turn breakdown:

  Turn 1: Architecture review
       Input         9,141        4,820        -47.3%
      Output           823          823          0.0%
        Cost       $0.0286       $0.0196        -31.5%
        Time          6.8s          6.1s        -10.3%

  Turn 2: Implementation
       Input         6,341        3,323        -47.6%
      Cached             0        7,203
      Output         1,024        1,024          0.0%
        Cost       $0.0225       $0.0142        -36.9%
        Time          5.5s          5.0s         -9.1%

  Highlights (vs first arm):
    tokenpak: 7,203 cache-read tokens
    tokenpak: 7,339 fewer input tokens (47.4% compression)
    tokenpak: $0.0173 saved (33.8% cheaper)

  Proof ID: prf_3f7a1c2e
```

### Column legend

| Column | Meaning |
|---|---|
| `direct-api` (arm 1 / baseline) | Calls sent directly to the provider with no proxy |
| `tokenpak` (arm 2) | Same calls routed through `tokenpak serve` |
| `vs [1]` | Percentage delta from arm 1 (baseline). **Negative = arm 2 is cheaper/fewer/faster.** A −33.8% cost means tokenpak cost 33.8% less than direct API on this workload. |
| `Cache-read tokens` | Prompt-cache hits; these tokens are billed at a fraction of full input price. No delta column is shown because cache hits are inherently asymmetric between arms. |

The `vs [1]` column is the primary signal. Negative values on cost, input tokens, and latency rows indicate improvement. A positive delta on output tokens would indicate a regression (tokenpak should never change output token counts — if it does, check your config).

---

## Scenarios

Scenarios are Markdown files with YAML frontmatter and `## Turn N` headings. The built-in `default` scenario asks two coding questions that stress test compression on repeated context.

**Scenario format:**

```markdown
---
name: My Scenario
model: claude-sonnet-4-6
provider: anthropic
max_tokens: 4096
---

## Turn 1: Initial question

Your first prompt here...

## Turn 2: Follow-up

Your follow-up that references context from Turn 1...
```

Store custom scenarios at `~/.tokenpak/prove/scenarios/<name>.md`. Run `tokenpak prove list` to confirm they are discovered.

---

## Past results

Each proof run saves a JSON result to `~/.tokenpak/prove/results/`. The proof ID (e.g. `prf_3f7a1c2e`) is shown in the report footer and can be passed to `tokenpak prove show <proof_id>` to replay the report at any time.

---

## See also

- [CLI Reference — `tokenpak prove`](../cli-reference.md#tokenpak-prove)
- [Telemetry & Dashboard](telemetry.md) — ongoing savings tracking via `tokenpak savings`
