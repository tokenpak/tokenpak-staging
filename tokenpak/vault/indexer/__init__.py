"""Dynamic vault indexer (writer + async consumer).

Implements the design in
``01_PROJECTS/tokenpak/initiatives/2026-05-16-dynamic-vault-indexing/02-design.md``.

The package is split into:

- ``append_log``  — synchronous per-record writer (proxy + vault watcher).
- ``lock``        — advisory flock(2) helpers + atomic-write helpers.
- ``checkpoint``  — resumable position persistence for the indexer process.
- ``telemetry``   — counters, latency, lag-gauge helpers (offline, no network).
- ``indexer``     — async drain loop (skeleton at this milestone).

Public-safe defaults: nothing in this tree calls out over the network; the
``vault.indexer.enable`` proxy-config flag defaults false per Std 20 §2.
"""

SCHEMA_VERSION = 1
"""Append-log record schema version (design §1.3)."""

__all__ = ["SCHEMA_VERSION"]
