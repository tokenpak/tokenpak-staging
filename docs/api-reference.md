# API Reference

TokenPak exposes a REST API on the proxy port for telemetry, session management, and admin operations.

Base URL: `http://localhost:8766`

---

## Authentication

Most endpoints require no authentication for local use. Admin and team endpoints require the `X-Admin-Token` header:

```bash
curl -H "X-Admin-Token: your-admin-secret" http://localhost:8766/v1/...
```

---

## Telemetry

### `GET /v1/telemetry/summary`

Overall statistics summary.

**Response:**
```json
{
 "total_requests": 847,
 "total_tokens_in": 2140000,
 "total_tokens_saved": 891000,
 "total_cost_usd": 10.42,
 "compression_rate": 0.416,
 "since": "2026-01-01T00:00:00Z"
}
```

---

### `GET /v1/telemetry/sessions`

List recorded sessions with optional filters.

**Query Parameters:**

| Param | Type | Description |
|-------|------|-------------|
| `since` | date | Start date (ISO 8601) |
| `until` | date | End date |
| `model` | string | Filter by model name |
| `agent` | string | Filter by agent name |
| `min_cost` | float | Minimum request cost (USD) |
| `compressed_only` | bool | Only compressed requests |
| `limit` | int | Max results (default: 100) |
| `offset` | int | Pagination offset |

**Response:**
```json
{
 "sessions": [
 {
 "id": "sess_abc123",
 "timestamp": "2026-03-05T14:23:11Z",
 "model": "claude-3-5-sonnet-20241022",
 "agent": "cali",
 "tokens_in": 4231,
 "tokens_in_compressed": 2847,
 "tokens_out": 612,
 "cost_usd": 0.0041,
 "compression_rate": 0.327,
 "recipe": "python-strip-comments",
 "latency_ms": 1243
 }
 ],
 "total": 23,
 "has_more": false
}
```

---

### `GET /v1/telemetry/export`

Export session data.

**Query Parameters:**

| Param | Type | Description |
|-------|------|-------------|
| `format` | string | `csv` or `json` (default: `json`) |
| `since` | date | Start date |
| `until` | date | End date |

**Response:** CSV or JSON file download.

---

### `GET /v1/telemetry/team`

Team-wide aggregated stats. Requires admin token.

**Response:**
```json
{
 "team_stats": {
 "total_requests": 1243,
 "total_cost_usd": 45.20,
 "total_tokens_saved": 3100000,
 "active_agents": 3
 },
 "by_agent": [
 {
 "agent": "cali",
 "requests": 542,
 "cost_usd": 19.40,
 "tokens_saved": 1320000
 }
 ]
}
```

---

### `GET /v1/telemetry/agents/{agent_id}`

Per-agent detail. Requires admin token.

**Response:**
```json
{
 "agent": "cali",
 "requests_today": 14,
 "cost_today_usd": 0.42,
 "requests_month": 542,
 "cost_month_usd": 19.40,
 "budget_remaining_usd": 80.60,
 "compression_rate": 0.41,
 "top_models": [
 { "model": "claude-3-5-sonnet", "requests": 380, "cost_usd": 14.20 }
 ]
}
```

---

## Session Replay

### `GET /v1/replay/list`

List replayable sessions.

**Query Parameters:**

| Param | Type | Description |
|-------|------|-------------|
| `last` | int | Return last N sessions |
| `agent` | string | Filter by agent |

---

### `POST /v1/replay/{session_id}`

Replay a session with optional overrides.

**Request body:**
```json
{
 "compress": false,
 "model": "gpt-4o-mini",
 "diff": true
}
```

**Response:** Replay result with optional diff vs original.

---

## Proxy Status

### `GET /v1/status`

Proxy health and stats.

**Response:**
```json
{
 "status": "ok",
 "version": "0.1.1",
 "uptime_seconds": 86400,
 "compression": {
 "enabled": true,
 "mode": "hybrid",
 "threshold_tokens": 4500
 },
 "session": {
 "requests": 23,
 "tokens_saved": 18341,
 "cost_usd": 0.042
 }
}
```

---

### `GET /health`

Structured health check. Returns the proxy status, uptime, version, session
counters, connection-pool metrics, and circuit-breaker state.

**No authentication required.**

Pass `?deep=true` to additionally include a per-provider status list, process
memory (`rss_mb`), and disk-available (`available_gb`) figures.

**Response:**
```json
{
 "status": "ok",
 "uptime_seconds": 3600,
 "version": "1.0.0",
 "requests_total": 23,
 "requests_errors": 0,
 "compression_ratio_avg": 0.41,
 "is_degraded": false,
 "is_shutting_down": false,
 "in_flight_requests": 0,
 "timestamp": "2026-03-16T19:10:00Z",
 "connection_pool": {
 "http2_enabled": true,
 "active_providers": ["anthropic"]
 },
 "circuit_breakers": {
 "enabled": true,
 "any_open": false,
 "providers": {}
 }
}
```

**Top-level `status` values:**

| Value | Meaning |
|-------|---------|
| `ok` | Proxy nominal |
| `degraded` | Degradation tracker active (e.g. a provider circuit is open or errors are elevated) |
| `shutting_down` | Proxy is draining in-flight requests for graceful shutdown |

The endpoint always responds `200`; the operational state is carried in the
`status` field (plus the `is_degraded` / `is_shutting_down` booleans), not the
HTTP code. The top-level `status` is therefore `ok` (never `healthy`); the
string `"ok"` is also reused for nested per-component health, such as each
circuit-breaker provider entry.

---

### `GET /ready`

Kubernetes/Docker readiness probe. Returns `200` only when the proxy has fully initialised and is accepting requests. Returns `503` during startup or graceful shutdown. No authentication required. Response time < 50ms (no I/O).

**Response (ready):**
```json
{ "ready": true, "status": "ready" }
```

**Response (503 — startup):**
```json
{ "ready": false, "status": "starting_up" }
```

**Response (503 — shutdown):**
```json
{ "ready": false, "status": "shutting_down" }
```

---

### `GET /v1/health`

> **Deprecated** — use `GET /health` instead (see above).

Detailed health check (legacy).

**Response:**
```json
{
 "proxy": "ok",
 "database": "ok",
 "index": "ok",
 "compression_pipeline": "ok",
 "version": "0.1.1"
}
```

---

## Budget

### `GET /v1/budget/status`

Current budget status.

**Response:**
```json
{
 "monthly_usd": 50.0,
 "spent_usd": 23.4,
 "remaining_usd": 26.6,
 "pct_used": 46.8,
 "alert_at_pct": 80,
 "on_exceeded": "warn"
}
```

---

## Models

### `GET /v1/models`

List of distinct models seen in session history.

**Response:**
```json
{
 "models": [
 "claude-3-5-sonnet-20241022",
 "gpt-4o",
 "gpt-4o-mini"
 ]
}
```

---

## Routing

### `GET /v1/routing/rules`

List active routing rules.

**Response:**
```json
{
 "rules": [
 {
 "pattern": ".*test.*",
 "model": "gpt-4o-mini",
 "created": "2026-03-01T10:00:00Z"
 }
 ]
}
```

---

### `POST /v1/routing/test`

Test which model a prompt would be routed to.

**Request:**
```json
{
 "prompt": "write unit tests for auth.py"
}
```

**Response:**
```json
{
 "model": "gpt-4o-mini",
 "matched_rule": ".*test.*",
 "fallback": "claude-3-5-sonnet-20241022"
}
```
