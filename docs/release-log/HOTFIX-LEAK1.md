---
title: TokenPak 1.31.0+leak1 hotfix — proxy thread, connection and memory leak
version: 1.31.0+leak1
release_type: local-hotfix
base: v1.31.0
status: built, not installed
owner: TokenPak
---

# TokenPak 1.31.0+leak1 — proxy lifecycle hotfix

A local hotfix build of 1.31.0 for hosts where the proxy grows threads, open
`monitor.db` descriptors and memory until it is restarted, and spends cores
of CPU while doing so. It is not a public release: nothing is published,
pushed or installed by this build. This page describes the install an owner
may approve later, and how to roll it back.

## What it fixes

On a busy host the proxy was observed at 336-945 live threads with only a
handful of open client sockets, 190-679 open `monitor.db` descriptors, 0.7-2.5
GB RSS and about two cores of CPU, growing until restart. Causes and fixes:

1. **Session economics polled without a bound.** Every companion status line
   posts `/v1/messages/session-economics` about every two seconds and gives
   up after half a second. Each evaluation re-read the whole request ledger
   (its cache key changed on every write, so it never hit), and the proxy
   kept evaluating after the client had gone, one thread per request. Once an
   evaluation took longer than the poll interval, threads, ledger parses and
   open connections piled up.
   - Concurrent requests for the same session now share one evaluation, at
     most two evaluations run at once, a request that cannot start within one
     second gets `503` with `Retry-After`, and a request whose client already
     hung up is skipped.
   - The calibration corpus is maintained incrementally from rows appended
     since the last read and always equals a full parse.
2. **SQLite connections closed only by the garbage collector.** On Python
   3.11+ a dropped connection stays open until a full cyclic collection, and
   while the WAL writer is open SQLite keeps every closed connection's
   descriptor for reuse, so the descriptor count is the most connections ever
   open at once. Every connection on a request or background path is now
   closed in `finally`/`closing()`; per-thread caches close their connection
   when the thread exits.
3. **Request bodies kept and re-parsed.** The cache-invalidator detector kept
   the raw last body of up to 100 sessions (1-3 MB each, no byte cap) and
   parsed the previous body again on every POST. It now keeps a compact
   signature under a 32 MiB cap and parses each body once.
4. **Client disconnects logged as tracebacks.** Each abandoned status poll
   printed a full traceback; under load those queued request threads on
   stderr.
5. **TLS context per upstream client.** Each new upstream client reloaded the
   CA bundle; one verified context is now shared.

## Build

| Item | Value |
| --- | --- |
| Source branch | `lane-LEAK1-proxy-lifecycle`, based on tag `v1.31.0` |
| Source commit | `d794c15f3f18a33e0a61865a8f03f2511f6f7b00` |
| Wheel | `tokenpak-1.31.0+leak1-py3-none-any.whl` |
| SHA-256 | `d3ac845b9880e5b25ba044b1bd9522e04ba5b579111c1feef5d87d7f88ae1244` |
| Built with | `python -m build --wheel --no-isolation` from a clean `git archive` of the source commit, `pyproject.toml` version set to `1.31.0+leak1` |

The wheel differs from the 1.31.0 release wheel only in the 39 changed and 2
added modules listed in the source commits; its `Requires-Dist` lines are
identical.

### Pairing with TokenPak Pro 0.6.1

The hotfix pairs with the same `tokenpak_paid` 0.6.1 wheel as 1.31.0:

- Pro 0.6.1 declares `tokenpak==1.31.0`. Under PEP 440 a local version
  (`1.31.0+leak1`) satisfies `==1.31.0`, so `pip` accepts the pair.
- Pro's runtime compatibility check reads `tokenpak/version.json` and
  `tokenpak.__version__`; both still say `1.31.0`, and the check returns
  `allowed` against the extracted wheels.
- `pip show tokenpak` reports `1.31.0+leak1`; `/health` and
  `tokenpak --version` report `1.31.0`. The pending-update detector ignores
  local versions, so the status line does not announce an update.

### New settings (all optional)

| Variable | Default | Effect |
| --- | --- | --- |
| `TOKENPAK_ECONOMICS_MAX_CONCURRENT` | `2` | Distinct session-economics evaluations allowed at once |
| `TOKENPAK_ECONOMICS_WAIT_S` | `1.0` | Seconds a request waits for a slot or a shared evaluation before `503` |
| `TOKENPAK_CACHE_INVALIDATOR_MAX_BYTES` | `33554432` | Total size of the cache-invalidator session cache |

Visible behaviour change: when the economics slots are busy, a status-line
refresh gets `503` and shows its previous or stale value for that refresh.

## Install plan (requires owner approval; not performed)

The proxy unit runs `~/.venvs/tokenpak-current/bin/python -m tokenpak.proxy`,
and `tokenpak-pro-daemon.service` runs from the same `tokenpak-current`
environment. `~/.local/state/tokenpak/releases/` is used read-only for the
dependency wheels.

1. **Pre-checks.**
   - `readlink ~/.venvs/tokenpak-current` prints the 1.31.0 environment
     (`~/.venvs/tokenpak-1.31.0-pro0.6.1`); record it for rollback.
   - No staged release is pending, so the unit's apply-at-launch hook will not
     repoint the link at restart:
     `test ! -e ~/.local/state/tokenpak/pending-release.json`.
   - Record a baseline for the running proxy: `pid=$(systemctl --user show
     -p MainPID --value tokenpak-proxy.service)`, then `grep -E
     'VmRSS|Threads' /proc/$pid/status` and `ls -l /proc/$pid/fd | grep -c
     'monitor.db$'`.
2. **Verify the artifacts.**
   - `sha256sum tokenpak-1.31.0+leak1-py3-none-any.whl` matches the value
     above.
   - `sha256sum ~/.local/state/tokenpak/releases/oss1.31.0-pro0.6.1/artifacts/tokenpak_paid-0.6.1-py3-none-any.whl`
     matches the value recorded for the 1.31.0 install.
3. **Build the new environment beside the current one.**

   ```bash
   A=~/.local/state/tokenpak/releases/oss1.31.0-pro0.6.1/artifacts
   H=<directory holding the hotfix wheel>
   N=~/.venvs/tokenpak-1.31.0+leak1-pro0.6.1
   python3.12 -m venv "$N"
   "$N/bin/pip" install --no-index --find-links "$H" --find-links "$A" \
       "tokenpak==1.31.0+leak1" "tokenpak_paid==0.6.1"
   "$N/bin/pip" check
   "$N/bin/python" -I -c "import tokenpak, tokenpak_paid.compatibility as c; \
       print(tokenpak.__version__, c.check_in_process_compatibility())"
   ```

   Expect `1.31.0` and `allowed=True`.
4. **Flip the link atomically.**

   ```bash
   ln -sfn "$N" ~/.venvs/tokenpak-current.next
   mv -T ~/.venvs/tokenpak-current.next ~/.venvs/tokenpak-current
   ```

5. **Restart.** `systemctl --user restart tokenpak-proxy.service`. Restart
   `tokenpak-pro-daemon.service` in the same window so both units run the same
   environment; if it is left running, it keeps the 1.31.0 code with the same
   Pro 0.6.1 until its next restart.
6. **Post-install checks.**
   - `curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8766/health`
     prints `200` within 30 seconds.
   - `"$N/bin/pip" show tokenpak | grep ^Version` prints `1.31.0+leak1`.
   - At 5, 30 and 60 minutes, sample the proxy with the commands from step 1
     (RSS, threads, `monitor.db` descriptors). Expected on a busy host: the
     descriptor count levels off at about twice the number of concurrent
     clients plus a few and stops rising; threads track open client
     connections plus about six background threads instead of climbing; RSS
     levels off instead of climbing hour over hour. Treat a descriptor count
     still rising at 60 minutes, more than about 100 threads, or RSS above
     the memory guard's threshold as a failed install and roll back.

## Rollback

1. Flip the link back to the recorded 1.31.0 environment:

   ```bash
   ln -sfn ~/.venvs/tokenpak-1.31.0-pro0.6.1 ~/.venvs/tokenpak-current.next
   mv -T ~/.venvs/tokenpak-current.next ~/.venvs/tokenpak-current
   ```

2. `systemctl --user restart tokenpak-proxy.service` (and the Pro daemon if it
   was restarted onto the hotfix).
3. `/health` returns `200`. The hotfix environment can stay in place for a
   later attempt. No database, configuration or schema is changed by the
   hotfix, so rollback needs no data step.

## Validation

Full detail, commands and the parity table are in
`reports/proxy-lifecycle-leak.md`.

**Soak** (`tests/proxy/test_proxy_leak_soak.py`): a real proxy process and a
local fake upstream, no network; 2,000 requests from 16 concurrent clients
(streaming and not, 5% with 1-3 MB bodies, 10% disconnecting mid-stream),
four status lines polling session economics every 2 s with a 0.5 s timeout,
and one dashboard tab, against a 20,000-row ledger.

| Measure (proxy process) | 1.31.0 | 1.31.0+leak1 |
| --- | --- | --- |
| Requests completed in the 20-minute window | 98 of 2,000 | 2,000 of 2,000 (572 s) |
| Client requests timed out (600 s) | 32 | 0 |
| Live threads: baseline / peak / 90 s after load | 5 / 2,214 / 2,216 | 5 / 36 / 9 |
| `monitor.db` descriptors: baseline / peak / end | 5 / 2,127 / 2,129 | 3 / 13 / 13 |
| RSS MB: baseline / peak / end | 109 / 1,867 / 1,916 | 86 / 238 / 219 |
| CPU per completed request | 30,925 ms | 368 ms |

The test passes on the hotfix and fails on 1.31.0.

**Session economics**, one evaluation after a ledger write, 150,000-row
ledger: 1.31.0 71.6 s CPU and 109 MB peak (under tracemalloc); hotfix
0.68-0.95 s and 1.0 MB under tracemalloc, 0.37-0.64 s without it.

**Tests**: 38 added regression tests pass on the hotfix; 36 of them fail on
1.31.0 (the other two are guards). The required offline suite shows no test
that passes on 1.31.0 and fails on the hotfix (see the report).
