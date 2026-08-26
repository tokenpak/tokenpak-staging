---
---

Add per-request start, time-to-first-byte, and stream-duration facts,
incremental output usage, and a read-only `/inflight` endpoint. Existing proxy
databases migrate additively with nullable columns, and existing rows retain
their prior values.
