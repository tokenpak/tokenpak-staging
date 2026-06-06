"""TokenPak Dispatch — OSS workflow-control layer (Standards Delta v0).

This package hosts the Dispatch record schemas (Pydantic v2 models + JSON
Schema exports) and the capability registry that underpin TokenPak Dispatch's
v0.1-alpha. It is the schema foundation authored by P-SCHEMA-01; runtime
modules (FrontDock intake, Run Ledger, routing, station runner, Gatehouse) land
in dependent Phase B packets (P-FRONTDOCK-01, P-LEDGER-01, P-CONTEXT-01,
P-WORKERS-01, P-GATEHOUSE-REVIEWER-01).

Technical namespace (Standards Delta v0 §2): module ``tokenpak/orchestration/
dispatch/``, CLI verb ``tokenpak dispatch``, MCP prefix ``dispatch.*``,
on-disk root ``~/.tpk/dispatch/`` (pending a path-config amendment). Dispatch
records are internal execution records, NOT Paks.
"""

from __future__ import annotations

from tokenpak.orchestration.dispatch.models import (
    DISPATCH_RECORD_MODELS,
    PakSuffixCollisionError,
    load_dispatch_models,
)
from tokenpak.orchestration.dispatch.registry.capabilities import (
    DISPATCH_CAPABILITIES,
    UnknownCapabilityError,
    validate_capabilities,
)

__all__ = [
    "DISPATCH_RECORD_MODELS",
    "PakSuffixCollisionError",
    "load_dispatch_models",
    "DISPATCH_CAPABILITIES",
    "UnknownCapabilityError",
    "validate_capabilities",
]
