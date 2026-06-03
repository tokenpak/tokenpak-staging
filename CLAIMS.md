# TokenPak Claims Proof

This file maps every public-facing product claim to a reproducible evidence
command, the CI coverage that guards it, the date it was last verified, the
caveats that bound the claim, and the owner accountable for keeping it true.

**Purpose:** the release-readiness gate (`make release-readiness`) reads this
file. If a claim shipped in README/marketing material is not represented here
with a current `last_verified` date, the gate flags it.

**Convention:** keep claims terse and stable. When the README changes a
headline number, update the corresponding row here in the same PR.

| Claim | Evidence command | CI / workflow coverage | Last verified | Caveats | Owner |
|---|---|---|---|---|---|
| **30–50% token reduction on real agent workloads** (`README.md` headline) | `make benchmark-headline` | `.github/workflows/benchmarks.yml` (headline benchmark gate) | 2026-06-01 | Numbers are corpus-dependent. Measured on the canonical DevOps + agent corpora fixed in `tests/benchmarks/`. Out-of-corpus workloads can land anywhere in or near this range; tail latency and savings vary per recipe. | Kevin / release manager |
| **<50 ms added latency** (`README.md` "Context compression" bullet) | `pytest tests/benchmarks/test_latency.py -v` (where present) or recipe-specific latency tests | `.github/workflows/benchmarks.yml` | 2026-06-01 | Measured single-stage on localhost loopback. Real-world latency includes the proxy network hop and the underlying provider; this number is the compressor cost only. | Kevin / proxy maintainer |
| **Local-first / no cloud** (`README.md` headline + "no credentials stored") | `bash scripts/audit.sh` (leakage check) + `python3 scripts/audit_internal_leakage.py` | `.github/workflows/repo-hygiene.yml`, `.github/workflows/public-layout-check.yml`, `.github/workflows/identity-language-check.yml` | 2026-06-01 | Refers to the OSS product surface. Optional Pro tiers and the dashboard may add cloud-backed features explicitly opted into. | Kevin / TokenPak maintainer |
| **No credentials stored** (`README.md` headline + per-product spec) | `pytest -m quick tests/test_quick_suite.py -k credential` + manual: search `tokenpak/` for any disk write touching API keys | `.github/workflows/ci.yml` (full test suite includes credential-passthrough tests) | 2026-06-01 | Applies to TokenPak itself. The proxy passes the user's credential through to the upstream provider on each call but never persists it. The user's own client SDK may still cache its credential per its own conventions. | Kevin / security owner |
| **One-command Claude Code setup** (`README.md` 30-second demo) | `tokenpak integrate claude-code --apply` (against a sandbox `~/.claude/settings.json`); `python3 scripts/integration_detector.py` to confirm detection signals | `.github/workflows/integration.yml` | 2026-06-01 | "One command" applies after `pip install tokenpak` and assumes Claude Code is already installed. The integrate command edits `~/.claude/settings.json`; rollback is `tokenpak integrate claude-code --undo`. | Kevin / integrations owner |
| **Works with Claude Code, Cursor, Cline, Continue.dev, Aider, OpenAI SDK, Anthropic SDK, LiteLLM, Codex** (`README.md` "Works with" block) | `tokenpak integrate` (lists supported clients) | `.github/workflows/integration.yml` | 2026-06-01 | "Works with" means TokenPak has an `integrate` shim and a tested integration recipe. Provider-side feature surface is unchanged — model-specific limitations (e.g. Codex ChatGPT OAuth unsupported models) still apply, and are surfaced via `tokenpak doctor`. | Kevin / integrations owner |
| **50 built-in compression recipes** (`README.md` "What's included") | `ls recipes/` and `tokenpak recipes list` (where exposed) | `.github/workflows/ci.yml` (recipe-format tests in `tests/`) | 2026-06-01 | Recipes are YAML; the count refers to shipped, named recipes in `recipes/`. Custom recipes loaded from user config are not included in the headline number. | Recipe maintainer |
| **Free tier covers compression, integration, routing, cost tracking, vault indexing, CLI + proxy, A/B + replay** (`README.md` Pricing table) | `python3 -m tokenpak --help` lists all free-tier commands; `tokenpak status` confirms no paid gate | `.github/workflows/ci.yml` + `tests/license/` (license-tier guard tests) | 2026-06-01 | Pricing applies to the OSS Free tier defined in `pyproject.toml` and the public docs. Pro/Team features are separately licensed in `tokenpak-paid`. | Kevin |
| **Benchmark reproduction by anyone** (`README.md` "Reproduce: make benchmark-headline") | `make benchmark-headline` in a fresh clone | `.github/workflows/benchmarks.yml` | 2026-06-01 | Requires Python 3.10+ and a checked-out copy of the repo. The benchmark uses the corpora bundled under `tests/benchmarks/`. Hardware variance is documented in `docs/benchmarks/`. | Benchmark owner |

---

## How this table is enforced

- `make release-readiness` checks that `CLAIMS.md` exists and is non-empty. The
  `claims` section of the report contributes to the go/no-go recommendation.
- Per-claim **evidence command** should run green in a clean checkout. When a
  command no longer reproduces the claim, the row is the source of truth for
  whether to downgrade the claim text in README or fix the implementation.
- **CI / workflow coverage** names the GitHub Actions workflow(s) that gate
  the claim on every push/PR. If a claim has no CI coverage, it must have a
  caveat that explains why (e.g. requires live provider credentials).
- **Last verified** is a date, not a commit SHA. Update it whenever the
  evidence command is re-run and confirmed green.
- **Owner** is the human (or fleet role) accountable for keeping the claim
  true. The owner is the escalation point if the evidence command starts
  failing in CI.

## Adding a new claim

1. Add a row to the table above with all six columns populated.
2. Add (or extend) an evidence command — a make target, a script, a pytest
   selector, or a documented procedure. The command MUST be runnable from a
   fresh clone.
3. Wire CI coverage in `.github/workflows/` if it isn't already covered.
4. Set `last_verified` to today's date.
5. Open the PR. Reviewer checks that the README and CLAIMS.md agree.

## Removing a claim

Remove or rewrite the README sentence first, then delete the corresponding row
here. Never let a row referring to a removed claim go stale — a row with a
stale `last_verified` is a worse failure mode than no row at all.
