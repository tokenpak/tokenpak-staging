"""Minimal notification route policy."""

from __future__ import annotations

from dataclasses import dataclass, field

from tokenpak.notifications.event import NotificationEvent


@dataclass(frozen=True)
class RouteRule:
    channel_id: str
    severities: set[str] = field(default_factory=set)
    categories: set[str] = field(default_factory=set)
    sources: set[str] = field(default_factory=set)
    audiences: set[str] = field(default_factory=set)
    topics: set[str] = field(default_factory=set)
    muted: bool = False

    def matches(self, event: NotificationEvent) -> bool:
        return (
            _matches(self.severities, event.severity)
            and _matches(self.categories, event.category)
            and _matches(self.sources, event.source)
            and _matches(self.audiences, event.audience)
            and _matches(self.topics, event.topic)
        )


@dataclass(frozen=True)
class RouteDecision:
    event_id: str
    status: str
    channel_id: str | None = None
    reason: str = ""


class RoutePolicy:
    """Match events to channels, defaulting to durable record-only."""

    def __init__(self, rules: list[RouteRule] | None = None) -> None:
        self.rules = rules or []

    def decide(self, event: NotificationEvent) -> list[RouteDecision]:
        matches = [rule for rule in self.rules if rule.matches(event)]
        if not matches:
            return [RouteDecision(event_id=event.id, status="record_only", reason="no matching route")]
        decisions: list[RouteDecision] = []
        for rule in matches:
            status = "muted" if rule.muted else "pending"
            reason = "matched muted route" if rule.muted else "matched route"
            decisions.append(
                RouteDecision(
                    event_id=event.id,
                    status=status,
                    channel_id=rule.channel_id,
                    reason=reason,
                )
            )
        return decisions


def _matches(allowed: set[str], value: str | None) -> bool:
    return not allowed or (value is not None and value in allowed)
