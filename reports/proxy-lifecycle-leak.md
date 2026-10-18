# Proxy lifecycle leak: implementation report

**Overall: LEAK-1, LEAK-2 and LEAK-3 done; LEAK-4 done for the small fix, with
the remaining per-request CPU documented below.** The soak test passes on the
head and fails on v1.31.0, every added test passes on the head, and the parity
run shows no test that passes on the base and fails on the head. A local wheel
is built. Nothing was installed, published or pushed, no service was touched,
and no live store was opened. Independent QA, landing and installation are
separate work.

Branch: `lane-LEAK1-proxy-lifecycle`.
Base: tag `v1.31.0` (`3d31e5d318d831db525334f96c87a6048c890aad`).
Head (code and tests): `d794c15f3f18a33e0a61865a8f03f2511f6f7b00`. The commit after it adds
only this report and the hotfix notes.
Report path: `reports/proxy-lifecycle-leak.md` (the repository's validation-artifact directory).
Hotfix notes: `docs/release-log/HOTFIX-LEAK1.md` (the release-notes directory).

## Base verification

The tag's `tokenpak/` tree was compared, read-only, with the RECORD of the
installed 1.31.0 wheel (sha256 `4a6f1513…7bde6a`): 1,099 of 1,099 packaged files
match. The one byte difference is `tokenpak/telemetry/data/pricing_catalog.json`,
a symlink in git whose target (`tokenpak/models/data/seed_catalog.json`) is
byte-identical to the wheel's copy. Every file this branch changes therefore
starts from exactly the code that is installed.

## Results by ID

| ID | Status | Commit | Regression tests (fail on v1.31.0, pass on head) |
| --- | --- | --- | --- |
| LEAK-1 | done | `22147af7bd` | `tests/proxy/test_sqlite_lifecycle.py` (13), `tests/proxy/test_sqlite_lifecycle_audit.py` (1 + 1 allowlist guard) |
| LEAK-2 | done | `0afacad6fd` | `tests/proxy/test_economics_gate.py` (7) |
| LEAK-3 | done | `b7ea25a8c0` | `tests/proxy/test_leak_memory_bounds.py` (13 + 1 equivalence guard) |
| LEAK-4 | done (small fix); remaining CPU documented | `2360b0757c` | `tests/proxy/test_connection_pool_tls_context.py` (2) |
| Soak | passes on head, fails on v1.31.0 | `cbd937d885` | `tests/proxy/test_proxy_leak_soak.py` (`needs_proxy`, `slow`) |
| Release gate | public-API snapshot (+7 additive symbols) and changesets | `d794c15f3f` | `scripts/release_gate/gen_api_snapshot.py --check` passes |

Guards: the allowlist guard and the corpus-equivalence test also pass on the
base, as intended (they check that the audit's allowlist is current and that
the incremental corpus equals a full parse).

## Diagnosis

Reproduced in a soak of v1.31.0 against a local fake upstream (see Soak). Three
mechanisms combine; the first drives the other two.

1. **Session-economics pile-up (threads, CPU, memory).** Each companion status
   line posts `/v1/messages/session-economics` every ~2 s with a 0.5 s
   timeout. Each call re-read and parsed the whole request ledger
   (`session_forecast_calibration._corpus_for` → `_parse_corpus`: `fetchall`,
   no limit; its cache key `(MAX(id), COUNT(*))` changes on every write, so it
   never hit). Measured per call after a ledger write: 10.3 s CPU and 14 MB
   peak at 20,000 rows, 71.6 s and 109 MB at 150,000 rows (both under
   tracemalloc). The proxy serves every request on its own thread
   (`proxy/server.py: _ThreadedHTTPServer.process_request` → `_handle`) and
   kept evaluating after the client had hung up. Once one evaluation outlasted
   the poll interval, request threads grew without bound, each holding open
   ledger connections and its own parse; the sockets sit in CLOSE-WAIT, which
   is why live thread counts far exceeded established connections.
2. **SQLite descriptors parked by the WAL writer (descriptors).** On Python
   3.11+ a connection that is dropped without `close()` lives until a full
   cyclic collection (its statement cache refers back to it). Separately,
   while any connection in the process holds a lock on `monitor.db` — the
   monitor's persistent WAL writer always does — SQLite's unix VFS does not
   close the descriptor of a closed connection but parks it for reuse.
   Verified directly: with a WAL writer open, 30 connections opened and closed
   leave 30 descriptors (per open mode, read-write and read-only separately)
   until the writer closes. The `monitor.db` descriptor count is therefore the
   most connections ever open at once, which is why it stays constant while
   threads rise and fall. Unclosed connections waiting for the collector and
   the economics pile-up raised that high-water mark.
3. **Retained bodies and re-parsing (memory).** The cache-invalidator detector
   kept the raw body of each session's last request (100 sessions, no byte
   cap; 1-3 MB each) and parsed the previous and the current body on every
   POST. The vault BM25 search opened a connection with a 16 MB page cache per
   search and never closed it.

Other findings: per-thread connection caches (`threading.local`) in
`telemetry/budget.py`, `cost_tracker.py`, `proxy_storage.py`, `replay.py`,
`storage.py`, `storage_base.py` and `core/registry` left each finished thread's
connection open, and `TelemetryDB._all_conns` held them forever; the companion
journal and budget stores had no `close()`, and the proxy built one per
`/tpk/v1` request; every client disconnect printed a full traceback.

## Changes

### LEAK-1: deterministic SQLite lifecycle

- Every `sqlite3.connect` outside the CLI is closed on success and error
  paths (`finally` or `closing()`); `with sqlite3.connect()` (which only
  commits) now also closes. 81 sites in 38 files changed (one new helper
  module).
- Per-thread caches hold a `ThreadConnection` (`tokenpak/_sqlite_lifecycle.py`)
  next to the cached connection; CPython drops a thread's local storage when
  the thread exits, the holder's reference count reaches zero and it closes
  the connection. `TelemetryDB` also removes the connection from
  `_all_conns`.
- Companion `JournalStore` and `BudgetTracker` gain `close()`; the proxy closes
  the store it builds for each `/tpk/v1` request.
- Coverage of every `sqlite3.connect` in `tokenpak/`: 138 direct calls (103
  outside the CLI, 35 in CLI modules) plus calls of local connection
  factories. The
  static audit (`test_sqlite_lifecycle_audit.py`) classifies every site
  outside `tokenpak/cli/` and `tokenpak/_cli_core.py` and fails on any that is
  not closed in `finally`/`closing()`, returned to a caller that does, or an
  allowlisted bounded cache (each allowlist entry names its owner and bound:
  the monitor's single writer, per-thread holders, instance-owned connections
  with `close()`). It reports 81 findings on v1.31.0 and none on the head.
  CLI command modules (35 calls) are unchanged: they run in a
  short-lived process and the operating system releases everything at exit.

### LEAK-2: bounded threads

- Thread creator: `proxy/server.py: _ThreadedHTTPServer.process_request`
  starts one thread per accepted connection; the threads that outlived their
  clients were `_handle` threads inside `_handle_session_economics`.
- `tokenpak/proxy/economics_gate.py`: identical concurrent requests share one
  evaluation; at most `TOKENPAK_ECONOMICS_MAX_CONCURRENT` (2) distinct
  evaluations run; a request that cannot start, or whose shared evaluation
  does not finish, within `TOKENPAK_ECONOMICS_WAIT_S` (1.0 s) gets `503` with
  `Retry-After`. Threads this endpoint can hold: the cap plus requests arriving
  within the wait.
- A request whose client already hung up is not evaluated.
- Client disconnects (`BrokenPipeError`, `ConnectionResetError`) are no longer
  printed as tracebacks; under load those queued request threads on stderr.

### LEAK-3: bounded memory

- Cache-invalidator: the session cache stores a compact signature (canonical
  tools, system, thinking, tool_choice, image flag), parses each body once,
  and is capped at `TOKENPAK_CACHE_INVALIDATOR_MAX_BYTES` (32 MiB default,
  least recently used first; an entry larger than the cap is not kept).
- Session-economics corpus: maintained incrementally from rows with
  `id > last max id` inside the same snapshot transaction; a refresh
  re-derives only sessions that received rows; per-session samples are
  memoised (4,096 entries). Deletions, a replaced file, a schema change, an
  out-of-order row or an evicted session that changes fall back to a full
  parse, which streams the cursor. The result always equals a full parse
  (randomised append/delete/out-of-order test). Bound: per-turn totals across
  sessions outside the window are capped at one million (8 bytes each); past
  it the oldest out-of-window sessions drop their totals, and a full parse
  restores them if they are ever needed.
- Per call after a ledger write, 150,000-row ledger: v1.31.0 71.6 s CPU and
  109 MB peak under tracemalloc; head 0.68-0.95 s and 1.0 MB under
  tracemalloc, 0.37-0.64 s CPU without it.

### LEAK-4: CPU

- Fixed (small): upstream clients share one verified TLS context; each new
  client previously reloaded the CA bundle (0.2-0.5 s CPU per new client).
- Soak CPU per completed request: v1.31.0 30,925 ms (98 requests completed);
  head 368 ms (2,000 completed). Same mix, DLP off in both (see Soak).
- Per-endpoint proxy CPU on the head (20,000-row ledger, one request at a
  time, DLP on / off; measured before the final corpus-entry caching in
  LEAK-3, which only lowers the session-economics row):

  | Request | DLP on | DLP off |
  | --- | --- | --- |
  | `POST /v1/messages` stream, 64 KB | 233 ms | 193 ms |
  | 256 KB | 550 ms | 313 ms |
  | 1 MB | 2,503 ms | 1,192 ms |
  | 3 MB | 8,043 ms | 4,111 ms |
  | `POST session-economics` | 115 ms | 155 ms |
  | `GET /metrics/dashboard` | 435 ms | 546 ms |
  | `GET /savings`, `/cache-stats`, `/v1/sessions` | 24-48 ms | 24-37 ms |
  | `GET /stats`, `/health`, `/status`, `/recent` | 2-7 ms | 2-7 ms |

- Documented, not fixed (not small, or a behaviour or schema change):
  1. **Outbound DLP scan** (`security/dlp.py: DLPScanner.scan`, on by
     default in warn mode) runs every rule over the whole decoded body on every
     request, about 1.3 ms per KB; a Claude Code request carries the whole
     conversation, so the history is rescanned every turn. Proposal: scan only
     content not seen in the session's previous request, or cache verdicts by
     message digest.
  2. **Cache-poison scrub** (`proxy/cache_poison.py: strip_cache_poisons`)
     applies three regexes to every message and re-serialises on a match, on
     every request. Proposal: memoise per message digest.
  3. **`/metrics/dashboard`** runs one `GROUP BY` and then one full scan per
     top-20 session for the p50 latency (no `session_id` index); about 0.5 s
     at 20,000 rows, proportionally more on a large ledger, every 5 s while a
     dashboard tab is visible. Proposal: one query for all 20 sessions, or an
     index.
  4. **No index on `requests(session_id)`**: session economics, spend-guard
     session reads and the dashboard scan the table for one session (about
     0.5 s at 150,000 rows). Proposal: `CREATE INDEX IF NOT EXISTS` with a
     migration test and a telemetry-schema snapshot update (a schema change,
     so not in a hotfix).
  5. **Upstream client per TCP connection**: the pool keys session clients by
     `x-tokenpak-session-id`, `x-session-id` or `x-correlation-id`, else by the
     client's address and port. Claude Code sends none of those, so every new
     client connection builds a new upstream client. Keying by
     `X-Claude-Code-Session-Id` would change upstream connection topology, so
     it is left for a separate decision.
  6. **One thread per connection, unbounded for unmanaged requests.** Under
     CPU saturation every handler runs slower and abandoned clients leave
     threads that finish late. The economics gate removes the pathological
     case; a general admission bound would change behaviour for model traffic.

### Pro (`tokenpak_paid` 0.6.1)

No leak site on the proxy path is in Pro: the proxy process imports no Pro
module (Pro 0.6.1 registers CLI commands only). The same lifecycle audit run
over an extracted copy of the Pro wheel (read-only) finds, in short-lived CLI
commands, connections closed only on the success path, and one
instance-owned connection. Outline for the tokenpak-paid repo:

> **Title:** Close SQLite connections in `finally` in CLI command helpers
> **Sites:** `commands/_impls/budget.py: _get_spent, _budget_history,
> _get_model_daily_avg`; `commands/_impls/optimize.py: _analyze_model,
> _analyze_redundancy`; `commands/_impls/savings.py: _query_savings,
> _query_by_model`; `enterprise/audit.py: __init__` (instance-owned; confirm
> a `close()` path).
> **Impact:** low (process-lifetime CLI); hygiene, and required if any helper
> is ever called from a long-running process.
> **Acceptance:** the OSS audit test's classifier reports no finding over the
> Pro package.

## Soak

`tests/proxy/test_proxy_leak_soak.py` drives `tests/proxy/_leak_soak.py`
through a real proxy process and a local fake upstream (no network): 2,000
requests from 16 concurrent clients (streaming and non-streaming, 5% with 1-3
MB bodies, the rest 16-96 KB, 10% of clients disconnecting mid-stream), four
status lines polling session economics every 2 s with a 0.5 s timeout, one
dashboard tab polling 8 endpoints every 5 s, and a 20,000-row seeded ledger.
The DLP scan is off in the soak proxy (about 1 ms of CPU per KB, unrelated to
the lifecycle under test; with it on, 2,000 requests do not fit a bounded run
on one core). Documented bounds: `monitor.db` descriptors at most 2 × 21
clients + 8 = 50 and growing at most 4 from the halfway checkpoint to the end;
threads back within 4 of baseline within 90 s; RSS growth at most 600 MB.

| Measure (proxy process) | v1.31.0 (`3d31e5d318` + the test files) | head `d794c15f3f` |
| --- | --- | --- |
| Result | **fail**: load did not finish (1,637 s) | **pass** (621 s) |
| Requests completed in the 1,200 s window | 98 of 2,000 | 2,000 of 2,000 (572 s) |
| Client outcomes | 48 stream 200, 11 JSON 200, 7 disconnects, 32 timed out at 600 s | 1,459 stream 200, 311 JSON 200, 230 disconnects, 0 errors |
| Live threads: baseline / peak / after drain | 5 / 2,214 / 2,216 (no drain in 92 s) | 5 / 36 / 9 (2.5 s) |
| `monitor.db` descriptors: baseline / peak / end | 5 / 2,127 / 2,129 | 3 / 13 / 13 |
| `monitor.db` descriptors at 25 / 50 / 75 / 100% of requests | n/a (98 done) | 13 / 13 / 13 / 13 |
| RSS MB: baseline / peak / end | 109 / 1,867 / 1,916 | 86 / 238 / 219 (+133) |
| CPU per completed request | 30,925 ms | 368 ms |
| Status-line polls | 2,330, all timed out | 916: 2 answered, 914 timed out* |
| Dashboard polls | 87 OK, 121 timed out | 232 OK, 8 timed out |
| Connections open at the end, by site | 154 corpus readers, 38 session-row readers, 26 resolver probes, 16 rolling-cap readers, others | the monitor writer only |

\* With 16 clients sending back to back, the head proxy runs at one full core,
so a status poll rarely gets an answer inside 0.5 s; the poll is abandoned,
the proxy skips the evaluation and the thread ends. On v1.31.0 every abandoned
poll still ran a full-ledger evaluation.

Earlier exploratory runs on v1.31.0 showed the same signature: 945 threads,
835 `monitor.db` descriptors and 700 MB RSS after 400 requests.

## Parity

Rule: the required suite on a throwaway `git archive` export of the base
(`v1.31.0`, `3d31e5d318`) and of the head (`d794c15f3f`). A failure already
present on the base or a network-only collection error is not a failure of the
head; a test that passes on the base and fails on the head is; every added
test must pass.

- Selection (the repository's offline selection, `tests/TEST-ENV-MATRIX.md`):
  `pytest tests/ -m 'not needs_proxy and not needs_webhook and not
  needs_internal_alerts and not needs_cali_env and not needs_fast_host'
  --continue-on-collection-errors -n 3 --timeout=120`, `PYTHONHASHSEED=0`,
  `env -i`, temporary `HOME`/`TMPDIR`, audit-hook network guard. Base and head
  ran at the same time (07:09-07:52 local) on a host at load 13-15 on 8 cores.
- Node-by-node comparison of the JUnit output, then a serial rerun of every
  differing node on both exports.

| Check | Base v1.31.0 | Head `d794c15f3f` | Result |
| --- | --- | --- | --- |
| Required offline suite, full run | 9,447 passed, 55 failed, 33 errors, 354 skipped, 1 xfailed (2,334 s) | 9,473 passed, 67 failed, 33 errors, 354 skipped, 1 xfailed (2,576 s) | 13 nodes pass on base and fail on head; 1 the reverse |
| Collection errors (network-only: `tiktoken` `cl100k_base` download) | 11 modules | the same 11 modules | identical |
| Serial rerun of the 14 differing nodes | 2 passed, 12 failed | 2 passed, 12 failed | **0 deterministic regressions** |
| — the 12 that fail on both | `test_agent_vault_indexer` (12 cases): `requests.ConnectionError` downloading the `tiktoken` encoding, blocked by the guard | same | network-only; order-dependent in the parallel run |
| — `test_durable_reservations::test_workload_snapshot_refuses_newer_unresolved_send_and_ledger_mismatch` | passes serially | passes serially; 5 of 5 further serial runs pass | load flake (`Monitor.flush(timeout=3)`), not a regression |
| — `test_cli_serve_smoke::test_cli_serve_with_port_flag` | failed in the full run; passes serially | passes | flake on the base |
| Failing on both in the full run | 65 nodes | 65 nodes | pre-existing |
| Added tests (6 files) | — | 38 nodes in the offline selection, 38 passed | PASS |
| Added tests run against base code | 36 failed, 2 passed (the two guards) | 38 passed | regressions are detected |
| Soak test (`needs_proxy`, outside the offline selection) | failed | passed | see Soak |
| Network denials, full run | 117 (73 to port 8766, 44 DNS) | 129 (73 to port 8766, 56 DNS) | pre-existing tests only; the 12 extra DNS lookups are the vault-indexer `tiktoken` downloads |

## Wheel

`dist-leak1/tokenpak-1.31.0+leak1-py3-none-any.whl` in the worktree (untracked),
built from `d794c15f3f` with `python -m build --wheel --no-isolation` from a
clean `git archive`, `pyproject.toml` version set to `1.31.0+leak1`.

- SHA-256: `d3ac845b9880e5b25ba044b1bd9522e04ba5b579111c1feef5d87d7f88ae1244`.
- Against the 1.31.0 release wheel: 1,101 vs 1,099 `tokenpak/` files; 2 added
  (`tokenpak/_sqlite_lifecycle.py`, `tokenpak/proxy/economics_gate.py`), 39
  changed (exactly the modules the commits change), none removed;
  `Requires-Dist` identical.
- Pro 0.6.1 declares `tokenpak==1.31.0` (METADATA); `1.31.0+leak1` satisfies
  it under PEP 440. With the two wheels extracted side by side (no install),
  `tokenpak.__version__` is `1.31.0`, `importlib.metadata.version("tokenpak")`
  is `1.31.0+leak1`, Pro's `check_in_process_compatibility()` returns
  `allowed=True, ok`, and the pending-update parser ignores the local version.

## Validation commands

- Isolation: every run uses `env -i` with a temporary `HOME`, `TMPDIR` and
  `TOKENPAK_HOME`, and an audit-hook network guard (loopback only; listening
  ports present at the start of the run, including 8766, denied). Soak
  proxies use `/dev/shm` homes. No live store, service, proxy or coordination endpoint
  was opened.
- Lint: `ruff check` and `ruff format --check` on every changed Python file;
  `tests/lint` and `tests/release` pass.
- Release gate: `gen_api_snapshot.py --check` and
  `gen_telemetry_schema.py --check` pass on the head.

## Follow-ups

1. LEAK-4 items 1-6 above.
2. Landing: the branch carries the public-API snapshot and changesets; the
   workbench PR, independent QA and installation are separate tasks.
3. Pro outline above.
