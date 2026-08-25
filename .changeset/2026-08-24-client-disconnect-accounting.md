---
---

Classify downstream client disconnects as `client_disconnect` instead of
synthetic 502 or provider failures. Genuine upstream errors retain their
existing failure path, while disconnects no longer pollute breaker and error
accounting.
