# SPDX-License-Identifier: Apache-2.0
"""Recall storage foundation (OSS Phase 1).

This package owns the local-first SQLite schema, migration runner, and
transparent OSS ranking helper used by the recall surface.

The storage foundation remains open: tables, the FTS5 virtual table,
the forward-only migration runner, and a deterministic metadata ranker.
Automatic capture, advanced cross-source recall, hydration, and agentic
handoff remain outside this package.

Notes:
    - Recall storage foundation is OSS Phase 1 (MultiPak Pro architecture).
    - TIP capabilities must land in OSS before the Pro daemon can use them
      (Pro tier architecture).
"""

from __future__ import annotations

from tokenpak.companion.recall.ranker import rank_paks
from tokenpak.companion.recall.schema import SCHEMA_VERSION
from tokenpak.companion.recall.store import (
    LIST_LIMIT_DEFAULT,
    LIST_LIMIT_MAX,
    RISK_FLAG_SEVERITIES,
    PakListFilters,
    PakListResult,
    PakRow,
    ReasonCodeEntry,
    RecallStore,
    RiskFlagEntry,
    default_recall_db_path,
    open_recall_store,
)

__all__ = [
    "LIST_LIMIT_DEFAULT",
    "LIST_LIMIT_MAX",
    "PakListFilters",
    "PakListResult",
    "PakRow",
    "ReasonCodeEntry",
    "RecallStore",
    "RISK_FLAG_SEVERITIES",
    "RiskFlagEntry",
    "SCHEMA_VERSION",
    "default_recall_db_path",
    "open_recall_store",
    "rank_paks",
]
