# Pre-send extension call site: implementation report

**Overall: complete.** The reviewed OSS patch is applied as real commits, its 17
tests pass, every OSS invariant was checked with evidence, the patch applies
cleanly to both downstream lanes, and suite parity with the base holds with no
deterministic regression. Independent QA, release and installation remain
separate work. No push, publish, install, host configuration or service change
was made, and no model or provider was called.

Branch: `lane-REROUTE-oss-hook`.
Base: `ff8ecd0a950b0f916f745a15abc1d7b256606a56`.
Code head: `0e32a65c8a5e56298b6253b2bffbe2bab6bfb462` (the next commit adds only this report).

## Source patch

| Item | Value |
| --- | --- |
| File | `reports/reroute-presend/oss-hook.patch` in the Pro repository |
| Taken at | `e495f27e5c05a7f90da6454fcfdabf931fd02ee5` (the reviewed repair head) |
| Same blob at | `d4f7957c98f801e9ad0e721c0409e7706eb89bd3` (blob `54503fed631d`; unchanged since the patch was reviewed) |
| SHA-256 | `c0e568608a63d54f0bf8377dd1d6b5e2c32e8898abebe1f7d3b5d7bcdef680cd` |
| Size | 834 lines; 3 files, 775 insertions, 0 deletions |

## Commits

| Commit | Content |
| --- | --- |
| `04cd35b08e` | The patch, verbatim. `git apply --check -R` of the patch passes on this commit, its diff equals the patch apart from abbreviated index lines, and every resulting blob matches the patch's index line: `server.py` `bdc935d` to `d464c17`, `presend_hook.py` `acea67e`, `test_presend_hook.py` `511dd4c`. |
| `0e32a65c8a` | Two-line test change: the `X-Tokenpak-Agent` value sent and asserted in `test_extension_sees_no_credentials` becomes `agent-a`. |
| (this commit) | This report. |

**Why the one extra change.** The patch applies to the base without any change.
Its test used an internal agent name as the attribution value, and the
repository's identity-language check rejects that name in changed files, so the
lane could not land on its own. The value is arbitrary. The assertion is
unchanged: the header value must reach the extension as `agent_id`. The patch
commit is kept byte-exact so it can be checked against the source.

## What the patch adds

- `tokenpak/proxy/presend_hook.py`: the `tokenpak-presend/1` contract. It
  discovers one extension from the `tokenpak.presend` entry-point group, once
  per process. It calls the extension on a 4-thread pool with a hard timeout
  (`TOKENPAK_PRESEND_TIMEOUT_MS`, default 50) and applies a returned routing
  value by replacing only the JSON value of the top-level `model` and of the
  existing effort field. A side-effect-free Spend Guard policy check then runs
  on the rewritten request. `TOKENPAK_PRESEND_HOOK=0` disables the call site.
- `tokenpak/proxy/server.py`: records the guard outcome and the guard's own
  parse of the request. The call site runs after the guard allows the request
  and before the forwarding headers are built. The extension receives only an
  allowlist of session and attribution headers, never credentials. Once
  response usage is known, the same counts the proxy prices are reported to
  the extension's `observe_response`. These counts are labelled with their
  source. According to the Pro lane's review, OSS previously sent this
  payload as null.
- `tests/proxy/test_presend_hook.py`: 17 tests. 15 use a test-double
  extension; 2 drive the real Pro module when it is importable.

## Tests added by the patch

| Run | Result |
| --- | --- |
| Candidate, OSS only (Pro not importable) | 15 passed, 2 skipped (the Pro-only tests) |
| Candidate, Pro source at `e495f27` on `PYTHONPATH` (read-only export) | **17 passed** |
| T1 repair head + lane commits, with Pro | 17 passed |
| Routing data head + lane commits, with Pro | 17 passed |

All runs used an isolated `HOME` and `TMPDIR`, `PYTHONHASHSEED=0`, and an audit-hook network guard. The guard denies DNS for non-loopback names, every non-loopback connect, and loopback connects to ports already listening on the host. It recorded **0** denials in every run of this file.

## OSS invariants

| Invariant | Verdict | Evidence |
| --- | --- | --- |
| Pro absent or erroring: passthrough byte-identical | PASS | Differential run: 8 requests went through the base proxy and the candidate proxy to a local stub upstream. The requests were odd-whitespace and escaped-unicode JSON, no effort field, attribution headers, Responses protocol, non-JSON, JSON array, duplicate `model` keys, and a 200 KB body. For each, the run compared the upstream path, headers (minus `Host`) and body, and the client status and body. The base was identical across 2 runs. The candidate was identical to the base in all 9 modes: real discovery (no entry point installed), hook disabled by env with a rewriting extension, extension raises, extension times out, non-mapping result, non-routing keys, entry point fails to load, entry-point discovery raises, and stay. |
| Stay is byte-preserving | PASS | The stay mode saw 8 calls and was identical to the base. Patch tests also pass: `test_stay_on_the_claude_code_route_forwards_the_client_bytes`, `test_stay_matches_the_proxy_without_any_extension` and `test_real_pro_stay_is_byte_identical`. |
| Call site actually reached (control) | PASS | A rewriting extension saw 8 calls. It changed exactly the 5 valid-JSON requests, and in each only the `model` value changed. The non-JSON, array and duplicate-key bodies were refused (`rewrite_refused`) and forwarded unchanged. |
| OSS informs and guards; Pro decides | PASS | OSS only builds the request view, applies the named routing values and re-checks them with the guard. It holds no reroute policy, targets or prices. The rule that only a guard state of `allow` may reroute is applied by Pro (`presend_decision.py` stays on any other state). The test `test_only_routing_values_are_accepted` shows that OSS rejects anything other than `model`/`effort` values. |
| OSS default makes no reroute decision | PASS | OSS declares no `tokenpak.presend` entry point, and no entry point is installed on this host's interpreter. With none, `hooks()` returns `None` and the call site does nothing (differential mode "real discovery"). |
| Spend Guard hard stops still win | PASS | A request the guard blocks returns 402 before the call site; the extension is never called and nothing reaches the upstream (`test_hard_stop_blocks_before_the_extension`). A rewrite is re-checked with the same configuration against the new model's context window. Any verdict other than allow/warn refuses the rewrite and forwards the original (`test_rewrite_that_the_guard_would_block_is_refused`). That covers the context-window hard stop, the soft block and the legacy hard-block bands. |
| No network calls on the hot path | PASS | `presend_hook.py` imports no network module. The re-check reads the in-process model registry; the registry's discovery fetch is a separate background job that this path never starts. Entry-point discovery reads local package metadata once per process. The network guard recorded 0 denials across the patch tests and all 11 differential runs. |

### Observations for independent QA (no change made)

- **O-1 (LOW).** The guard re-check calls the policy without the session
  running cost and does not evaluate rolling caps. The opt-in session-cost and
  rolling-cap block bands are therefore not re-evaluated for the new model. The
  in-flight rolling-cap reservation still uses the original model's estimate.
  Hard stops are re-checked.
- **O-2 (LOW).** OSS does not check that a target model belongs to the request's
  provider. The control run showed OSS accepting a Responses-route rewrite to
  an Anthropic model id when the guard allowed it. The Pro repair validates
  target provider and protocol before deciding.
- **O-3 (INFO).** The `server.py` comment says only `allow` permits a reroute.
  The check that enforces this is in Pro, not OSS (see the invariant table).

## Apply results on downstream lanes

Checked with `git apply --cached --check` against a throwaway index for each
tree. No branch, ref or worktree was touched.

| Tree | Head | Patch | Lane commits | Notes |
| --- | --- | --- | --- | --- |
| Base | `ff8ecd0a950b` | clean, exact | clean | |
| T1 repair (`lane-T1-repair`) | `865dbb65763262fca517be1fb3ed4d979c3d9f62` | **clean** | clean | `server.py` hunks at offsets +2/+18/+23; no fuzz |
| Routing data (`lane-T2-routing-data`) | `60479d3c20299c887b872ace6fd2a135237523d8` | **clean** | clean | same `server.py` blob as T1 repair (`0852469`); same offsets |

**No conflicts.** The patched copies of both lanes also pass all 17 tests
(see above).

## Suite parity with the base

Rule: run the same selection on read-only exports of the base and the
candidate. A failure present on the base, or a network-only failure, is not a
regression. A test that passes on the base and fails on the candidate is a
regression, and every added test must pass. Every differing node is re-run
serially on both trees. Selection: the repository's hermetic offline markers,
`-m 'not needs_proxy and not needs_webhook and not needs_internal_alerts and
not needs_cali_env and not needs_fast_host'`, with
`--continue-on-collection-errors -n 3 --timeout=120` and the network guard.
That selection excludes `needs_proxy`, which the whole new test file carries,
so the proxy suite was also run with `needs_proxy` included. CI runs
`needs_proxy` tests.

| Run | Passed | Failed | Errors | Skipped | xfailed | Collection-error modules |
| --- | --- | --- | --- | --- | --- | --- |
| Base, full offline | 9459 | 72 | 36 | 353 | 1 | 10 (network-only) |
| Candidate, full offline | 9459 | 74 | 33 | 353 | 1 | same 10 |
| Base, `tests/proxy` incl. `needs_proxy` | 1554 | 57 | 17 | 10 | 1 | 5 (network-only) |
| Candidate, `tests/proxy` incl. `needs_proxy` | 1571 | 55 | 17 | 12 | 1 | same 5 |

| Node-level comparison | Full offline | Proxy incl. `needs_proxy` |
| --- | --- | --- |
| Nodes, base / candidate | 9898 / 9898 | 1629 / 1646 |
| Pass on base, fail on candidate (parallel run) | 17 | 4 |
| Fail on base, pass on candidate (parallel run) | 17 | 6 |
| Differing nodes re-run serially on both trees | 34: 14 pass on both, 20 fail on both | 10, three rounds each: intermittent failures on both trees (base 5/30 runs, candidate 2/30) |
| **Deterministic regressions** | **0** | **0** |
| Added tests | not selected (`needs_proxy`) | 17: 15 passed, 2 Pro-only skipped (17/17 with Pro) |

The 20 nodes that fail on both trees in the serial re-run need a tokenizer
download, which the guard denies (`openaipublic.blob.core.windows.net`). The
parallel-run differences came from load: the host load average was 13 to 17 on
8 cores while both suites ran together.

In the proxy re-run, the 4 nodes that failed only on the candidate in the
parallel run pass 3/3 serially on the candidate. The remaining failures are
timing flakes seen on both trees, in concurrency tests that wait on
`Monitor.flush` or an event:

- `test_conservative_context_reservation_blocks_concurrent_send` failed once
  on each tree.
- `test_repeated_local_native_refusals_do_not_mark_provider_unhealthy` failed
  once serially on the candidate, and on the base in the parallel run.
- `test_client_disconnect_after_attempt_keeps_hold_and_releases_only_probe`
  failed 2/3 on the base and 0/3 on the candidate.

## Reproduce

```text
git show <pro-sha>:reports/reroute-presend/oss-hook.patch | sha256sum
git apply --check -R oss-hook.patch               # on 04cd35b08e
git diff ff8ecd0a95 04cd35b08e -- . | diff - oss-hook.patch   # index lines only
pytest tests/proxy/test_presend_hook.py           # 15 passed, 2 skipped without Pro
PYTHONPATH=<pro-export> pytest tests/proxy/test_presend_hook.py   # 17 passed
```
