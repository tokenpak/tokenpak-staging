# SPDX-License-Identifier: Apache-2.0
"""Prompt Packing — high-level orchestration over the compression pipeline.

``PromptPackingService`` wraps the existing ``CompressionPipeline`` and
``ContextPack`` stages, producing a ``PromptPackingResult`` whose ``.pak``
attribute carries a TIP-conformant ``Pak`` instance.

Compression internals are composed, never renamed or relocated.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from tokenpak.tip.pak import (
    Pak,
    PakAuthority,
    PakConfidence,
    PakRetention,
    PakRetentionPolicy,
    PakScope,
    PakSource,
    PakSourceType,
    PakStatus,
    PakSubtype,
)

from .pack import ContextPack, PackBlock
from .pipeline import CompressionPipeline, PipelineResult


@dataclass
class CompressionMetadata:
    """Metadata from the compression stage of a prompt-packing run."""

    tokens_raw: int
    tokens_after: int
    tokens_saved: int
    savings_pct: float
    duration_ms: float
    stages_run: List[str]
    pipeline_result: Optional[PipelineResult] = field(default=None, repr=False)
    compile_report: Optional[Any] = field(default=None, repr=False)


@dataclass
class PromptPackingResult:
    """Output of ``PromptPackingService.pack()``.

    Attributes:
        pak: A TIP-conformant ``Pak`` carrying the packed content summary.
        text: The compiled output text ready for LLM consumption.
        compression_metadata: Stats from the compression pipeline.
    """

    pak: Pak
    text: str
    compression_metadata: CompressionMetadata


@dataclass
class PackingPolicy:
    """Controls how prompt packing behaves.

    All fields are optional; defaults produce a reasonable single-request
    packing run using the standard compression pipeline.
    """

    budget: int = 8_000
    quality_threshold: float = 0.5
    enable_dedup: bool = True
    enable_alias: bool = True
    enable_segmentation: bool = True
    enable_directives: bool = True
    enable_instruction_table: bool = True
    project: Optional[str] = None
    topic: Optional[str] = None


class PromptPackingService:
    """Orchestrates prompt packing for a single request.

    Composes the compression pipeline and the context-pack compiler,
    then wraps the result in a ``Pak``.
    """

    def __init__(
        self,
        *,
        default_budget: int = 8_000,
        default_quality_threshold: float = 0.5,
    ) -> None:
        self._default_budget = default_budget
        self._default_quality_threshold = default_quality_threshold

    def pack(
        self,
        prompt: str | List[Dict[str, Any]],
        policy: Optional[PackingPolicy] = None,
    ) -> PromptPackingResult:
        """Pack *prompt* through the compression pipeline and return a result.

        Parameters
        ----------
        prompt:
            Either a plain-text string or a list of message dicts
            (each with at least a ``"role"`` key).
        policy:
            Optional packing policy. ``None`` uses service defaults.
        """
        if policy is None:
            policy = PackingPolicy(
                budget=self._default_budget,
                quality_threshold=self._default_quality_threshold,
            )

        messages = _to_messages(prompt)
        t0 = time.perf_counter()

        pipeline = CompressionPipeline(
            enable_dedup=policy.enable_dedup,
            enable_alias=policy.enable_alias,
            enable_segmentation=policy.enable_segmentation,
            enable_directives=policy.enable_directives,
            enable_instruction_table=policy.enable_instruction_table,
        )
        pipeline_result = pipeline.run(messages)

        context_pack = ContextPack(
            budget=policy.budget,
            quality_threshold=policy.quality_threshold,
        )
        for i, msg in enumerate(pipeline_result.messages):
            content = msg.get("content", "")
            role = msg.get("role", "user")
            priority = "critical" if role == "system" else "medium"
            context_pack.add(
                PackBlock(
                    id=f"msg_{i}",
                    type=role,
                    content=content if isinstance(content, str) else str(content),
                    priority=priority,
                )
            )

        compiled = context_pack.compile()

        total_ms = (time.perf_counter() - t0) * 1000.0

        metadata = CompressionMetadata(
            tokens_raw=pipeline_result.tokens_raw,
            tokens_after=pipeline_result.tokens_after,
            tokens_saved=pipeline_result.tokens_saved,
            savings_pct=pipeline_result.savings_pct,
            duration_ms=round(total_ms, 2),
            stages_run=pipeline_result.stages_run,
            pipeline_result=pipeline_result,
            compile_report=compiled.report,
        )

        source_hash = hashlib.sha256(compiled.text.encode()).hexdigest()
        pak = Pak(
            pak_id=f"ppack-{uuid.uuid4().hex[:12]}",
            pak_type=PakSubtype.RECALL,
            title="Prompt Packing result",
            summary=f"Packed {metadata.tokens_raw} tokens to {metadata.tokens_after} ({metadata.savings_pct}% saved)",
            scope=PakScope(project=policy.project, topic=policy.topic),
            source=PakSource(
                platform="tokenpak",
                source_type=PakSourceType.TOOL_RESULT,
                created_at=datetime.now(timezone.utc).isoformat(),
                source_hash=source_hash,
            ),
            status=PakStatus.ACCEPTED,
            authority=PakAuthority.TOOL_RESULT,
            confidence=PakConfidence.HIGH,
            retention=PakRetentionPolicy(ttl=PakRetention.SESSION),
        )

        return PromptPackingResult(
            pak=pak,
            text=compiled.text,
            compression_metadata=metadata,
        )


PromptPacker = PromptPackingService


def _to_messages(prompt: str | List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if isinstance(prompt, str):
        return [{"role": "user", "content": prompt}]
    return list(prompt)


__all__ = [
    "CompressionMetadata",
    "PackingPolicy",
    "PromptPacker",
    "PromptPackingResult",
    "PromptPackingService",
]
