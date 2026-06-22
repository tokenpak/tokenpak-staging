# SPDX-License-Identifier: Apache-2.0
"""Non-routing external-tool TIP source-adapter interface + registry.

the spec category: a TokenPak component that *observes* usage events
surfaced by a third-party developer tool and projects them into
TIP-shaped **observed** records — without routing any model traffic and
without holding or injecting any credential.

Honesty contract (the spec, mandatory):

1. Records are labeled **TokenPak-observed** — the observed tool does
   NOT speak TIP natively and is never represented as emitting TIP.
2. No savings / percentage claims are attached to observed records —
   raw observed counts only.
3. The adapter is strictly **read-only** with respect to the observed
   tool: it never modifies the tool's invocation, config, credentials,
   state, or output.

Capability labels use the ``ext.<tool>.<feature>`` namespace per
the spec (pattern subset of
``tokenpak/tip/schemas/tip-capabilities.v1.json``). ``tip.*`` labels
are reserved for protocol-native capabilities and are rejected here.

OFF BY DEFAULT.  Nothing in this module runs unless::

    TOKENPAK_TIP_TOOL_ADAPTERS=1

Runtime discovery (no hardcoded tool enum):
tools register themselves via :func:`register_external_tool_source`
at import time.  :func:`discover_sources` finds registrants by

* importing every ``tokenpak.sources.*_tip_source`` module found via
  :mod:`pkgutil` (in-tree adapters; adding one is a new file, zero
  edits to this module), and
* importing each module named in the
  ``TOKENPAK_TIP_TOOL_ADAPTER_MODULES`` env var (comma-separated
  import paths; out-of-tree adapters, zero core change).

This interface is a deliberate **sibling** of
``tokenpak.sources.base_source.SourceAdapter`` — that ABC feeds the
vault BM25 content index with ``(content, Provenance)`` pairs, whereas
this one emits structured TIP observation records into the existing
``tokenpak tip`` surface.  Shoehorning TIP events into the content
ingestion return shape would corrupt both contracts.
"""

from __future__ import annotations

import hashlib
import importlib
import logging
import os
import pkgutil
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Type

logger = logging.getLogger(__name__)

ENV_FLAG = "TOKENPAK_TIP_TOOL_ADAPTERS"
ENV_EXTRA_MODULES = "TOKENPAK_TIP_TOOL_ADAPTER_MODULES"

#: ext-namespace subset of the TIP capability label pattern declared in
#: ``tokenpak/tip/schemas/tip-capabilities.v1.json``.
EXT_LABEL_RE = re.compile(r"^ext\.[a-z0-9._-]+$")

#: Tool slugs are a single lowercase token (they become the second label
#: segment: ``ext.<tool>.<feature>``).
TOOL_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

#: the spec — provenance claim string carried by every record.
OBSERVED_CLAIM = "tokenpak-observed"

#: pkgutil discovery convention: in-tree adapter modules end with this.
_MODULE_SUFFIX = "_tip_source"


def is_enabled(env: Optional[Dict[str, str]] = None) -> bool:
    """Master gate — off-by-default; enable with ``TOKENPAK_TIP_TOOL_ADAPTERS=1``."""
    source = env if env is not None else os.environ
    val = (source.get(ENV_FLAG) or "").strip().lower()
    return val in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Observed record
# ---------------------------------------------------------------------------

def _default_provenance() -> Dict[str, Any]:
    """the spec provenance block: observed/derived by TokenPak, never tool-native."""
    return {
        "observed_by": "tokenpak",
        "claim": OBSERVED_CLAIM,
        "tool_native_tip": False,
    }


@dataclass
class ObservedTIPRecord:
    """One TokenPak-observed TIP record projected from an external tool's surface.

    ``observed_usage`` carries raw observed counts only (e.g. token
    counters summed over the span).  Derived savings / percentage
    figures are forbidden absent a committed benchmark
    and have no field here on purpose.
    """

    tool: str
    labels: List[str]
    command: Optional[str] = None
    role: Optional[str] = None
    phase: Optional[str] = None
    session_id: Optional[str] = None
    source_path: Optional[str] = None
    first_timestamp: Optional[str] = None
    last_timestamp: Optional[str] = None
    observed_usage: Dict[str, int] = field(default_factory=dict)
    provenance: Dict[str, Any] = field(default_factory=_default_provenance)

    def __post_init__(self) -> None:
        if not TOOL_SLUG_RE.match(self.tool):
            raise ValueError(f"invalid external tool slug: {self.tool!r}")
        validate_ext_labels(self.tool, self.labels)
        claim = self.provenance.get("claim")
        if claim != OBSERVED_CLAIM:
            raise ValueError(
                "the spec honesty contract: provenance.claim must be "
                f"{OBSERVED_CLAIM!r}, got {claim!r}"
            )
        if self.provenance.get("tool_native_tip"):
            raise ValueError(
                "the spec honesty contract: records must never claim "
                "tool-native TIP emission"
            )

    @property
    def record_id(self) -> str:
        """Stable content-derived identifier."""
        seed = "|".join([
            self.tool,
            self.session_id or "",
            self.command or "",
            self.first_timestamp or "",
            self.last_timestamp or "",
        ])
        return f"{self.tool}.{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:16]}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "record_id": self.record_id,
            "tool": self.tool,
            "labels": sorted(self.labels),
            "command": self.command,
            "role": self.role,
            "phase": self.phase,
            "session_id": self.session_id,
            "source_path": self.source_path,
            "first_timestamp": self.first_timestamp,
            "last_timestamp": self.last_timestamp,
            "observed_usage": dict(self.observed_usage),
            "provenance": dict(self.provenance),
        }


def validate_ext_labels(tool: str, labels: Iterable[str]) -> None:
    """Enforce the spec: ``ext.<tool>.*`` only, never ``tip.*``."""
    prefix = f"ext.{tool}."
    for label in labels:
        if label.startswith("tip."):
            raise ValueError(
                f"label {label!r}: tip.* is reserved for protocol-native "
                "capabilities — use ext.<tool>.* instead"
            )
        if not EXT_LABEL_RE.match(label):
            raise ValueError(
                f"label {label!r} does not match the ext capability "
                f"pattern {EXT_LABEL_RE.pattern}"
            )
        if not label.startswith(prefix):
            raise ValueError(
                f"label {label!r} is outside this tool's namespace "
                f"({prefix}*)"
            )


# ---------------------------------------------------------------------------
# Source interface
# ---------------------------------------------------------------------------

class ExternalToolTIPSource(ABC):
    """Abstract base for non-routing external-tool TIP source adapters.

    Subclasses set ``tool_slug`` and implement :meth:`collect` to walk
    their tool's observable surface (transcripts, state files, logs —
    read-only) and return :class:`ObservedTIPRecord` instances.  They
    run on-demand/batch only — no daemon (packet AC #2).
    """

    #: lowercase tool slug; becomes the ``ext.<tool>.*`` namespace.
    tool_slug: str = ""

    #: static ``ext.<tool>.*`` capability labels this adapter can emit
    #: (validated at registration time, without instantiation).
    static_capabilities: frozenset = frozenset()

    @abstractmethod
    def collect(self) -> List[ObservedTIPRecord]:
        """Discover this tool's observable inputs and return observed records."""
        ...

    def capabilities(self) -> frozenset:
        """``ext.<tool>.*`` capability labels this adapter can emit."""
        return self.static_capabilities

    def describe(self) -> Dict[str, Any]:
        return {
            "tool": self.tool_slug,
            "adapter": type(self).__name__,
            "capabilities": sorted(self.capabilities()),
        }


# ---------------------------------------------------------------------------
# Registry — populated by registration, never by enum (discovery stays dynamic)
# ---------------------------------------------------------------------------

_REGISTRY: Dict[str, Type[ExternalToolTIPSource]] = {}


def register_external_tool_source(
    cls: Type[ExternalToolTIPSource],
) -> Type[ExternalToolTIPSource]:
    """Register an adapter class (usable as a decorator).

    Validates the slug and the class-declared capability namespace.
    Re-registration of the same class is idempotent; a *different*
    class under an existing slug is rejected loudly.
    """
    slug = getattr(cls, "tool_slug", "") or ""
    if not TOOL_SLUG_RE.match(slug):
        raise ValueError(
            f"{cls.__name__}: tool_slug {slug!r} must match {TOOL_SLUG_RE.pattern}"
        )
    static_caps = getattr(cls, "static_capabilities", frozenset()) or frozenset()
    if static_caps:
        validate_ext_labels(slug, static_caps)
    existing = _REGISTRY.get(slug)
    if existing is not None and existing is not cls:
        raise ValueError(
            f"external tool slug {slug!r} already registered by "
            f"{existing.__name__}"
        )
    _REGISTRY[slug] = cls
    logger.debug("external_tool_tip: registered source tool=%s class=%s",
                 slug, cls.__name__)
    return cls


def unregister_external_tool_source(slug: str) -> None:
    """Remove a registered adapter (primarily for test isolation)."""
    _REGISTRY.pop(slug, None)


def registered_sources() -> Dict[str, Type[ExternalToolTIPSource]]:
    """Snapshot of the current registry (slug → adapter class)."""
    return dict(_REGISTRY)


def get_source(slug: str) -> Optional[Type[ExternalToolTIPSource]]:
    return _REGISTRY.get(slug)


def discover_sources(
    extra_modules: Optional[Iterable[str]] = None,
    env: Optional[Dict[str, str]] = None,
) -> Dict[str, Type[ExternalToolTIPSource]]:
    """Import adapter modules so they self-register; return the registry.

    Discovery is purely additive and import-error tolerant: a broken
    adapter module is skipped with a structured log line, never fatal.
    """
    import tokenpak.sources as _pkg

    for modinfo in pkgutil.iter_modules(_pkg.__path__):
        if not modinfo.name.endswith(_MODULE_SUFFIX):
            continue
        _safe_import(f"{_pkg.__name__}.{modinfo.name}")

    source = env if env is not None else os.environ
    configured = (source.get(ENV_EXTRA_MODULES) or "").strip()
    names = list(extra_modules or [])
    if configured:
        names.extend(n.strip() for n in configured.split(",") if n.strip())
    for name in names:
        _safe_import(name)

    return registered_sources()


def _safe_import(module_name: str) -> None:
    try:
        importlib.import_module(module_name)
    except Exception as exc:
        logger.warning(
            "external_tool_tip: skip adapter module=%s reason=import-failed "
            "error=%s", module_name, exc,
        )


# ---------------------------------------------------------------------------
# On-demand collection entry point (no daemon — packet AC #2)
# ---------------------------------------------------------------------------

def collect_observed_records(
    *,
    tool: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    force: bool = False,
) -> Dict[str, Any]:
    """Run discovery + collection across registered adapters, on demand.

    Off-by-default: unless ``TOKENPAK_TIP_TOOL_ADAPTERS`` is truthy (or
    ``force=True`` for explicit programmatic use), this returns a
    ``skipped`` envelope without importing, instantiating, or running
    any adapter — zero behavior change.
    """
    if not force and not is_enabled(env):
        return {
            "skipped": True,
            "reason": "disabled",
            "flag": ENV_FLAG,
            "records": [],
            "sources": [],
            "errors": [],
        }

    discover_sources(env=env)
    records: List[ObservedTIPRecord] = []
    ran: List[str] = []
    errors: List[str] = []
    for slug, cls in sorted(registered_sources().items()):
        if tool and slug != tool:
            continue
        try:
            adapter = cls()
            found = adapter.collect()
        except Exception as exc:
            logger.warning(
                "external_tool_tip: skip tool=%s reason=collect-failed error=%s",
                slug, exc,
            )
            errors.append(f"{slug}: {exc}")
            continue
        ran.append(slug)
        records.extend(found)

    return {
        "skipped": False,
        "records": records,
        "sources": ran,
        "errors": errors,
    }


__all__ = [
    "ENV_FLAG",
    "ENV_EXTRA_MODULES",
    "EXT_LABEL_RE",
    "OBSERVED_CLAIM",
    "ObservedTIPRecord",
    "ExternalToolTIPSource",
    "is_enabled",
    "validate_ext_labels",
    "register_external_tool_source",
    "unregister_external_tool_source",
    "registered_sources",
    "get_source",
    "discover_sources",
    "collect_observed_records",
]
