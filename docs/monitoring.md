# Monitoring TokenPak

> **`tokenpak monitor` is deprecated and no longer available.**
>
> The old single-page monitor dashboard was retired — its static assets were
> removed, so the command no longer starts a server. Running `tokenpak monitor`
> now prints a short deprecation notice pointing here, and remains reachable for
> one more minor version before it is removed.

## What to use instead

| You want… | Use |
|---|---|
| A real-time health dashboard (proxy status, throughput, errors, cost) | `tokenpak dashboard` |
| A one-shot proxy + savings snapshot | `tokenpak status` |

```bash
# Real-time health dashboard (replaces the retired monitor UI)
tokenpak dashboard

# Point-in-time proxy + savings snapshot
tokenpak status
```

Both read the same live proxy data the old monitor UI used, so no telemetry is
lost by switching.

## The telemetry store is unaffected

Only the **`monitor` command and its dashboard UI** were retired. The
`monitor.db` telemetry store that the proxy writes is the canonical source for
savings, cost, and per-model reporting and is untouched. It continues to back:

- `tokenpak dashboard` — real-time health dashboard
- `tokenpak status` — proxy + savings snapshot
- `tokenpak savings` / `tokenpak cost` / `tokenpak models` — reporting views
