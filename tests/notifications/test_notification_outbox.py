import json
import stat

import pytest

from tokenpak.notifications import (
    DeliveryAttemptLedger,
    DeliveryAttemptRecord,
    NotificationEvent,
    NotificationOutbox,
    RoutePolicy,
    RouteRule,
    default_outbox_path,
)
from tokenpak.notifications.ledger import default_ledger_path


def test_notification_event_appends_to_default_outbox(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKENPAK_HOME", str(tmp_path))

    event = NotificationEvent(
        source="agent-claude-worker",
        category="cycle.summary",
        severity="info",
        title="Cycle complete",
        body="Worker completed a cycle.",
        audience="operators",
        cycle_id="cycle-123",
        metadata={"exit_code": 0},
    )

    record = NotificationOutbox().append(event)

    outbox_path = default_outbox_path()
    assert outbox_path == tmp_path / "companion" / "notifications" / "outbox.jsonl"
    assert stat.S_IMODE(outbox_path.stat().st_mode) == 0o600
    assert json.loads(outbox_path.read_text().strip()) == record
    assert record["id"] == event.id
    assert record["category"] == "cycle.summary"
    assert record["metadata"] == {"exit_code": 0}


def test_notification_event_requires_supported_category_and_audience():
    with pytest.raises(ValueError, match="unsupported notification category"):
        NotificationEvent(
            source="cycle-deadman",
            category="unknown",
            severity="warning",
            title="Deadman",
            body="bad category",
            audience="operators",
        )

    with pytest.raises(ValueError, match="requires audience or topic"):
        NotificationEvent(
            source="cycle-deadman",
            category="cycle.deadman_alert",
            severity="warning",
            title="Deadman",
            body="missing route target",
        )


def test_route_policy_defaults_to_record_only():
    event = NotificationEvent(
        source="cycle-deadman",
        category="cycle.deadman_alert",
        severity="critical",
        title="Cycle stale",
        body="Worker is stale.",
        topic="runtime",
    )

    assert RoutePolicy().decide(event)[0].status == "record_only"


def test_route_policy_matches_and_mutes_routes():
    event = NotificationEvent(
        source="cycle-deadman",
        category="cycle.deadman_alert",
        severity="warning",
        title="Cycle stale",
        body="Worker is stale.",
        audience="operators",
    )
    policy = RoutePolicy(
        [
            RouteRule(
                channel_id="telegram",
                severities={"warning"},
                categories={"cycle.deadman_alert"},
                audiences={"operators"},
            ),
            RouteRule(channel_id="audit-only", categories={"cycle.deadman_alert"}, muted=True),
        ]
    )

    decisions = policy.decide(event)

    assert [decision.status for decision in decisions] == ["pending", "muted"]
    assert [decision.channel_id for decision in decisions] == ["telegram", "audit-only"]


def test_delivery_attempt_ledger_records_statuses(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKENPAK_HOME", str(tmp_path))

    ledger = DeliveryAttemptLedger()
    muted = ledger.record(
        DeliveryAttemptRecord(
            event_id="evt-1",
            channel_id="telegram",
            status="muted",
            metadata={"reason": "maintenance window"},
        )
    )
    skipped = ledger.record(
        DeliveryAttemptRecord(event_id="evt-2", channel_id=None, status="skipped")
    )

    ledger_path = default_ledger_path()
    assert ledger_path == tmp_path / "companion" / "notifications" / "deliveries.jsonl"
    assert ledger.read_records() == [muted, skipped]
