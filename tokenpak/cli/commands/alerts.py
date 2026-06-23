# SPDX-License-Identifier: Apache-2.0
"""alerts command — test and manage alert delivery channels."""

from __future__ import annotations

import json
import sys

from tokenpak.cli._messages import error

# Public surface of this command module. ``error`` is a re-import from the
# internal ``tokenpak.cli._messages`` helper module (never an intended public
# entrypoint) and is scoped out of the API snapshot.
__all__ = ["cmd_alerts_test"]


def cmd_alerts_test(args) -> None:
    """Test an alert delivery channel by sending a sample payload."""
    channel = args.channel
    success = False

    if channel == "webhook":
        if not args.url:
            print(error("--url is required for --channel webhook"), file=sys.stderr)
            sys.exit(1)
        from tokenpak.alerts.channels import webhook
        request_body = json.loads(
            webhook._build_payload(
                event="test",
                severity="info",
                message="TokenPak alert delivery test",
                source="tokenpak alerts test",
            ).decode()
        )
        print(f"→ POSTing to {args.url}")
        print(f"  Body: {json.dumps(request_body, indent=2)}")
        success = webhook.deliver(
            args.url,
            event="test",
            severity="info",
            message="TokenPak alert delivery test",
            source="tokenpak alerts test",
        )

    elif channel == "slack":
        if not args.webhook:
            print(error("--webhook is required for --channel slack"), file=sys.stderr)
            sys.exit(1)
        from tokenpak.alerts.channels import slack
        request_body = {"text": slack._build_text("test", "info", "TokenPak alert delivery test")}
        print(f"→ POSTing to {args.webhook}")
        print(f"  Body: {json.dumps(request_body, indent=2)}")
        success = slack.deliver(
            args.webhook,
            event="test",
            severity="info",
            message="TokenPak alert delivery test",
        )

    else:
        print(error(f"Unknown channel: {channel!r}. Use 'webhook' or 'slack'."), file=sys.stderr)
        sys.exit(1)

    if success:
        print("✅ Delivery succeeded")
    else:
        print(error("Delivery failed (check logs for details)"))
        sys.exit(1)
