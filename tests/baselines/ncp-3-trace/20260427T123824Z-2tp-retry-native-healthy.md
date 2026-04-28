# NCP-3 session-lane trace

**Generated**: 2026-04-27T12:38:24.640619+00:00
**Window**: 30.0 min (2026-04-27T12:08:24.640619+00:00 → 2026-04-27T12:38:24.640619+00:00)
**Claude Code event count in window**: 0

## Synthesis

- **Q1 — H2 session-id collapse: no_data.** Inconclusive — rerun with the §4 workload (2 concurrent tokenpak claude sessions) and re-inspect.
- **Q3 — retry events recorded:** 0. Either no retries occurred OR the schema didn't tag them. Settle visually via the §4 workload.

## Dimensions

### dim1_session_collapse

```json
{
  "collapse_ratio": null,
  "distinct_request_ids": 0,
  "distinct_session_ids": 0,
  "verdict": "no_data"
}
```

### dim2_time_clustering

```json
{
  "spans": [],
  "verdict": "insufficient_data"
}
```

### dim3_status_distribution

```json
{}
```

### dim4_per_session_durations

```json
{}
```

### dim5_provider_audit

```json
{
  "distribution": {},
  "i0_violation": false,
  "non_oauth_providers": []
}
```

### dim6_retry_count

```json
{
  "note": "Lower bound \u2014 current schema doesn't tag every retry. Real retry behavior may be higher; settle via the H4 'Retrying in 20s' visual evidence + the \u00a74.4 OAuth-fresh test.",
  "retry_event_lower_bound": 0
}
```

### dim7_token_usage

```json
{
  "available": true,
  "no_usage_rows": true
}
```

### dim8_interleaving

```json
{
  "interleave_score": null,
  "verdict": "insufficient_data"
}
```
