"""``ContextEnrichmentStage`` — policy-gated adapter-backed vault injection.

Runs in the ``routing`` slot of the pipeline. Adds retrieved vault context
through an injected ``FormatAdapter`` registry so provider-specific wire
shapes stay isolated outside the services pipeline.

Gate: only runs when
  - ``ctx.policy.injection_enabled`` is True,
  - ``ctx.policy.body_handling == "mutate"`` (byte-preserve routes are
    forbidden from body mutation), and
  - the resolved format adapter declares ``tip.cache.proxy-managed``.

Budget: injected content is truncated to
``ctx.policy.injection_budget_chars`` to stop runaway context on prompts
that would otherwise blow past the request token ceiling.

Relevance gate: trivial prompts (below
``ctx.policy.injection_min_query_tokens``) are skipped — injecting
context for a one-word "hi" wastes tokens and pollutes the cache.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from tokenpak.services.request import Request
from tokenpak.services.request_pipeline.stages import PipelineContext

logger = logging.getLogger(__name__)

_CACHE_CAPABILITY = "tip.cache.proxy-managed"


class _FormatAdapter(Protocol):
    """Structural subset of the provider format-adapter contract used here."""

    source_format: str
    capabilities: frozenset[str]

    def normalize(self, body: bytes) -> Any: ...

    def denormalize(self, canonical: Any) -> bytes: ...


class _AdapterRegistry(Protocol):
    """Structural registry contract injected by the transport layer."""

    def detect(
        self,
        path: str,
        headers: dict[str, str],
        body: bytes,
    ) -> _FormatAdapter | None: ...


def _request_path(request: Request) -> str:
    """Best-effort path/URL lookup for adapter detection."""
    metadata = request.metadata or {}
    for key in ("path", "target_path", "request_path", "url", "target_url"):
        value = metadata.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _format_vault_context(hits: list[str], budget: int) -> tuple[str, int]:
    """Format retrieved vault hits, respecting a character budget."""
    pieces: list[str] = []
    used = 0
    for hit in hits:
        if not hit:
            continue
        remaining = budget - used - 20
        if remaining <= 0:
            break
        snippet = hit if len(hit) <= remaining else hit[:remaining]
        pieces.append(snippet)
        used += len(snippet) + 20
    if not pieces:
        return "", 0
    return "[tokenpak vault context]\n" + "\n---\n".join(pieces), len(pieces)


class ContextEnrichmentStage:
    """Pipeline Stage — enrich requests with vault context when policy allows."""

    name = "routing"

    def __init__(
        self,
        retriever: Any | None = None,
        adapter_registry: _AdapterRegistry | None = None,
    ) -> None:
        """Create the enrichment stage.

        ``retriever`` accepts ``(query: str, top_k: int)`` and returns an
        iterable of strings. ``adapter_registry`` is injected by callers that
        own wire-format concerns (for example the proxy transport); leaving it
        unset makes the stage a no-op until composition wires the registry in.
        """
        self._retriever = retriever
        self._adapter_registry = adapter_registry

    def _build_retriever(self) -> Any | None:
        """Lazily construct the default vault retriever.

        Returns None if the vault subsystem isn't importable or no index
        exists — the Stage becomes a no-op in that case, which is the
        correct behavior pre-vault-init and in minimal test environments.
        """
        if self._retriever is not None:
            return self._retriever
        try:
            from tokenpak.vault.blocks import BlockStore
        except Exception:  # noqa: BLE001
            return None
        try:
            store = BlockStore.default()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            return None

        def _search(query: str, top_k: int) -> list[str]:
            try:
                results = store.search(query, top_k=top_k)
            except Exception:  # noqa: BLE001
                return []
            out: list[str] = []
            for result in results:
                text = getattr(result, "text", None) or getattr(result, "content", None)
                if text:
                    out.append(str(text))
            return out

        self._retriever = _search
        return self._retriever

    def _resolve_adapter(self, ctx: PipelineContext) -> _FormatAdapter | None:
        registry = self._adapter_registry
        if registry is None:
            return None
        try:
            return registry.detect(
                _request_path(ctx.request),
                ctx.request.headers or {},
                ctx.request.body or b"",
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("context_enrichment: adapter resolution failed: %s", exc)
            return None

    def apply_request(self, ctx: PipelineContext) -> None:
        policy = ctx.policy
        if policy is None or not policy.injection_enabled:
            return
        if policy.body_handling != "mutate":
            # Byte-preserve routes: client-side hook/MCP path is the only
            # enrichment surface. Record the skip so telemetry sees it
            # wasn't a bug.
            ctx.stage_telemetry.setdefault("routing", {})[
                "enrichment_skipped"
            ] = "byte_preserve"
            return

        body = ctx.request.body or b""
        adapter = self._resolve_adapter(ctx)
        if adapter is None:
            ctx.stage_telemetry.setdefault("routing", {})[
                "enrichment_skipped"
            ] = "adapter_unresolved"
            return
        if _CACHE_CAPABILITY not in adapter.capabilities:
            ctx.stage_telemetry.setdefault("routing", {})[
                "enrichment_skipped"
            ] = "capability_missing"
            return

        try:
            canonical = adapter.normalize(body)
        except Exception as exc:  # noqa: BLE001
            logger.debug("context_enrichment: normalize failed: %s", exc)
            ctx.stage_telemetry.setdefault("routing", {})[
                "enrichment_skipped"
            ] = "normalize_failed"
            return

        query = canonical.last_user_message_text()
        if not query:
            return

        # Relevance gate. Token estimate uses the same //4 heuristic as
        # the companion hook — good enough to reject trivial prompts.
        if len(query) // 4 < policy.injection_min_query_tokens:
            ctx.stage_telemetry.setdefault("routing", {})[
                "enrichment_skipped"
            ] = "below_min_query_tokens"
            return

        retriever = self._build_retriever()
        if retriever is None:
            ctx.stage_telemetry.setdefault("routing", {})[
                "enrichment_skipped"
            ] = "no_retriever"
            return

        try:
            hits = [str(hit) for hit in retriever(query, 5) if hit]
        except Exception as exc:  # noqa: BLE001
            logger.warning("context_enrichment: retriever failed: %s", exc)
            return

        if not hits:
            return

        injected, hit_count = _format_vault_context(hits, policy.injection_budget_chars)
        if not injected:
            return

        canonical_with_context = canonical.with_injected_system_prefix(injected)
        try:
            new_body = adapter.denormalize(canonical_with_context)
        except Exception as exc:  # noqa: BLE001
            logger.warning("context_enrichment: denormalize failed: %s", exc)
            return
        if not new_body:
            return

        ctx.request.body = new_body
        ctx.stage_telemetry.setdefault("routing", {}).update({
            "enrichment_applied": True,
            "format": adapter.source_format,
            "injected_chars": len(injected),
            "injected_hits": hit_count,
        })

    def apply_response(self, ctx: PipelineContext) -> None:
        return


__all__ = ["ContextEnrichmentStage"]
