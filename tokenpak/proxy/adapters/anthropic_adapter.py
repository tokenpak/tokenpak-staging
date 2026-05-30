"""Anthropic format adapter."""

from __future__ import annotations

import copy
import json
from typing import Any, Dict, Mapping, Optional

from .base import FormatAdapter, cacheable_fill_trace
from .canonical import CanonicalRequest


class AnthropicAdapter(FormatAdapter):
    source_format = "anthropic-messages"

    def detect(self, path: str, headers: Mapping[str, str], body: Optional[bytes]) -> bool:
        lower = {k.lower(): v for k, v in headers.items()}
        return "/v1/messages" in path or "x-api-key" in lower or "anthropic-version" in lower

    def normalize(self, body: bytes) -> CanonicalRequest:
        data = json.loads(body)

        consumed = {"model", "system", "messages", "tools", "stream"}
        generation: Dict[str, Any] = {}
        raw_extra: Dict[str, Any] = {}

        for key, value in data.items():
            if key in consumed:
                continue
            if key in {"max_tokens", "temperature", "top_p", "top_k", "stop_sequences", "metadata"}:
                generation[key] = value
            else:
                raw_extra[key] = value

        return CanonicalRequest(
            model=data.get("model", "unknown"),
            system=copy.deepcopy(data.get("system", "")),
            messages=copy.deepcopy(data.get("messages", [])),
            tools=copy.deepcopy(data.get("tools")),
            generation=generation,
            stream=bool(data.get("stream", False)),
            raw_extra=raw_extra,
            source_format=self.source_format,
        )

    def denormalize(self, canonical: CanonicalRequest) -> bytes:
        payload: Dict[str, Any] = {
            "model": canonical.model,
            "messages": copy.deepcopy(canonical.messages),
            "stream": canonical.stream,
        }
        if canonical.system not in (None, "", []):
            payload["system"] = copy.deepcopy(canonical.system)
        if canonical.tools is not None:
            payload["tools"] = copy.deepcopy(canonical.tools)

        payload.update(copy.deepcopy(canonical.generation))
        payload.update(copy.deepcopy(canonical.raw_extra))
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def inject_system_context(
        self,
        body: bytes,
        injection_text: Optional[str] = None,
        *,
        stable_text: Optional[str] = None,
        volatile_text: Optional[str] = None,
        trace_out: Optional[Dict[str, Any]] = None,
    ) -> bytes:
        """
        Inject context into the system prompt with correct cache boundaries.

        Two modes:

        - **Legacy** (positional ``injection_text``): the injected text is treated
          as a single VOLATILE block appended after the (cache_control-marked)
          original system. Behavior is byte-for-byte unchanged from before.

        - **two-layer cacheable-injection** (``stable_text`` / ``volatile_text`` keywords):
          a cacheable STABLE layer plus an uncached VOLATILE layer. Block order:
              [ original system… (cache_control) ,
                stable injection  (cache_control) ,
                volatile injection (NO cache_control) ]
          Caching is cumulative up to a breakpoint, so the original system + stable
          injection form a cacheable prefix that survives per-turn volatile churn
          (AC #5/#6). Volatile retrieval sits strictly after the breakpoint.

        When the request already carries cache_control with an explicit TTL (e.g.
        Claude Code CLI byte-preserved traffic), NO new cache_control markers are
        added — the client manages cache ordering and TokenPak must not claim its
        cache as proxy-attributable. ``trace_out`` (if provided) is populated with
        the stable/volatile layer hashes and the ``cache_origin`` attribution.
        """
        canonical = self.normalize(body)
        has_explicit_ttl = self._body_has_explicit_ttl(canonical)
        two_layer = stable_text is not None or volatile_text is not None

        if not two_layer:
            injection_text = injection_text or ""
            # Volatile injection block — intentionally NO cache_control
            volatile_block: dict = {"type": "text", "text": injection_text}
            cached = False
            if isinstance(canonical.system, str):
                if canonical.system:
                    stable_block: dict = {"type": "text", "text": canonical.system}
                    if not has_explicit_ttl:
                        stable_block["cache_control"] = {"type": "ephemeral"}
                        cached = True
                    canonical.system = [stable_block, volatile_block]
                else:
                    canonical.system = injection_text
            elif isinstance(canonical.system, list):
                if canonical.system and not has_explicit_ttl:
                    marked = list(canonical.system)
                    for i in range(len(marked) - 1, -1, -1):
                        blk = marked[i]
                        if isinstance(blk, dict) and blk.get("type") == "text":
                            if not blk.get("cache_control"):
                                marked[i] = dict(blk, cache_control={"type": "ephemeral"})
                            cached = True
                            break
                    canonical.system = marked
                elif canonical.system:
                    canonical.system = list(canonical.system)
                canonical.system.append(volatile_block)
            else:
                canonical.system = injection_text
            if trace_out is not None:
                origin = "client" if has_explicit_ttl else ("proxy" if cached else "unknown")
                cacheable_fill_trace(trace_out, "", injection_text, cache_origin=origin)
            return self.denormalize(canonical)

        # ---- two-layer cacheable-injection path ----
        stable_text = stable_text or ""
        volatile_text = volatile_text or ""

        if isinstance(canonical.system, str):
            blocks: list = [{"type": "text", "text": canonical.system}] if canonical.system else []
        elif isinstance(canonical.system, list):
            blocks = list(canonical.system)
        else:
            blocks = []

        proxy_cached = False
        if not has_explicit_ttl:
            # Breakpoint 1 — cache the original system prefix (durable across turns).
            for i in range(len(blocks) - 1, -1, -1):
                blk = blocks[i]
                if isinstance(blk, dict) and blk.get("type") == "text":
                    if not blk.get("cache_control"):
                        blocks[i] = dict(blk, cache_control={"type": "ephemeral"})
                    proxy_cached = True
                    break

        if stable_text:
            stable_block = {"type": "text", "text": stable_text}
            if not has_explicit_ttl:
                # Breakpoint 2 — extend the cacheable prefix over the stable layer.
                stable_block["cache_control"] = {"type": "ephemeral"}
                proxy_cached = True
            blocks.append(stable_block)

        if volatile_text:
            # Volatile retrieval — strictly after the breakpoint, never cached.
            blocks.append({"type": "text", "text": volatile_text})

        canonical.system = blocks
        if trace_out is not None:
            origin = "client" if has_explicit_ttl else ("proxy" if proxy_cached else "unknown")
            cacheable_fill_trace(trace_out, stable_text, volatile_text, cache_origin=origin)
        return self.denormalize(canonical)

    @staticmethod
    def _body_has_explicit_ttl(canonical) -> bool:
        """Check if any block in the request has cache_control with an explicit ttl."""
        for section in [canonical.system or [], canonical.messages or []]:
            items = section if isinstance(section, list) else []
            for item in items:
                if isinstance(item, dict):
                    cc = item.get("cache_control")
                    if isinstance(cc, dict) and cc.get("ttl"):
                        return True
                    # Check nested content blocks in messages
                    content = item.get("content")
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict):
                                cc = block.get("cache_control")
                                if isinstance(cc, dict) and cc.get("ttl"):
                                    return True
        return False

    def get_default_upstream(self) -> str:
        return "https://api.anthropic.com"

    def get_sse_format(self) -> str:
        return "anthropic-sse"
