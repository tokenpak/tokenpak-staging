---
---

Make the release-gate SQLite schema snapshot deterministic and complete. The
generator now materializes clean telemetry, Spend Guard, and proxy monitor
stores in an isolated temporary directory, so CI detects schema changes
without reading or depending on a user's local databases.
