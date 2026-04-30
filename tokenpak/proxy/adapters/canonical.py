"""Canonical request/response types for provider-agnostic proxy processing."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional, Union

SystemType = Union[str, List[Dict[str, Any]], None]


@dataclass
class CanonicalRequest:
    """Provider-neutral request structure used by proxy processing stages."""

    model: str
    system: SystemType = ""
    messages: List[Dict[str, Any]] = field(default_factory=list)
    tools: Optional[List[Dict[str, Any]]] = None
    generation: Dict[str, Any] = field(default_factory=dict)
    stream: bool = False
    raw_extra: Dict[str, Any] = field(default_factory=dict)
    source_format: str = "unknown"

    def last_user_message_text(self) -> str:
        """Return provider-neutral text for the last user message."""
        for msg in reversed(self.messages):
            if not isinstance(msg, dict) or msg.get("role") != "user":
                continue
            text = _content_to_text(msg.get("content", ""))
            if text:
                return text
        return ""

    def with_injected_system_prefix(self, injection_text: str) -> "CanonicalRequest":
        """Return a copy with ``injection_text`` prepended to system context."""
        system = copy.deepcopy(self.system)
        if isinstance(system, str):
            system = f"{injection_text}\n\n{system}" if system else injection_text
        elif isinstance(system, list):
            system = [{"type": "text", "text": injection_text}] + system
        else:
            system = injection_text
        return replace(self, system=system)


def _content_to_text(content: Any) -> str:
    """Convert canonical message content into text without provider branching."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            text = part.get("text")
            if isinstance(text, str):
                parts.append(text)
                continue
            input_text = part.get("input_text")
            if isinstance(input_text, str):
                parts.append(input_text)
        return "\n".join(parts)
    if isinstance(content, dict):
        text = content.get("text")
        if isinstance(text, str):
            return text
        input_text = content.get("input_text")
        if isinstance(input_text, str):
            return input_text
    return ""


@dataclass
class CanonicalResponse:
    """Provider-neutral response structure used for token usage extraction."""

    model: str = "unknown"
    usage: Dict[str, Any] = field(default_factory=dict)
    content: Any = None
    raw_extra: Dict[str, Any] = field(default_factory=dict)
    source_format: str = "unknown"
