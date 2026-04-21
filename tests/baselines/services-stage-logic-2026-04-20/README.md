# services/* pipeline-stage migration baseline (2026-04-20)

## Purpose

Pre-migration structural snapshot for Initiative 2026-04-20-tokenpak-services-pipeline-stage-logic. Each stage migration (S-PS-01..S-PS-05) diffs against this baseline.

## What's captured

- **public_symbols.json** — public-symbol sets for all 204 modules under `tokenpak.{proxy, services, compression, cache, routing, telemetry}.*`. Invariant: canonical public API must be preserved.
- **pytest_collect_stdout.txt** / `_returncode.txt` — full test-set identity. Invariant: post-migration is superset (new tests added are OK; no removals).
- **tip_conformance_stdout.txt** / `_returncode.txt` — self-conformance verdict. Invariant: bit-identical post-migration.
- **tokenpak_version.txt** — version under which baseline was captured.

## Phase D diff protocol

After each stage lands (S-PS-01..S-PS-05), run `scripts/diff_services_stage_baseline.py` (written in S-PS-01 or as part of the per-stage test harness). Zero diff on public_symbols + version + tip_conformance, superset-OK on pytest, is the per-stage merge gate.

## Live-traffic byte fidelity

Like P-AP-01, this baseline does NOT include live-provider byte captures — no examples/benchmarks/ harness, no provider creds in the capture session. Byte-fidelity of passthrough is enforced via the existing `tests/test_byte_fidelity.py` + `tests/proxy/test_passthrough.py` suites, which MUST stay green at every stage.

## Capture command

    python3 scripts/capture_services_stage_baseline.py

## Capture metadata

- Date: 2026-04-20
- Branch at capture: `feat/services-stage-logic`
- Parent branch: `feat/tip-1.0-phase-2-scaffold`
- Packages inventoried: 6 (proxy, services, compression, cache, routing, telemetry)
- Total modules: 204
- `pytest --co` exit: 0
- `tip-check` exit: 0
