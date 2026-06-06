# SPDX-License-Identifier: Apache-2.0
"""Tests for the centralised CLI message-prefix helpers (_messages.py).

The CLI error/warn/info surface uses capitalised plain prefixes with no emoji.
"""

from __future__ import annotations

from tokenpak.cli import _messages


def test_error_prefix_is_capitalised_plain():
    assert _messages.error("boom") == "Error: boom"


def test_warn_prefix_is_capitalised_plain():
    assert _messages.warn("careful") == "Warning: careful"


def test_info_prefix_is_capitalised_plain():
    assert _messages.info("fyi") == "Info: fyi"


def test_prefixes_contain_no_emoji_or_nonascii():
    for prefix in (_messages.ERROR_PREFIX, _messages.WARNING_PREFIX, _messages.INFO_PREFIX):
        assert prefix.isascii(), f"prefix {prefix!r} must be ASCII (no emoji)"
        assert prefix.endswith(":")


def test_helpers_preserve_message_body():
    msg = "path /tmp/x not writable (code 13)"
    assert _messages.error(msg).endswith(msg)
    assert _messages.warn(msg).endswith(msg)
    assert _messages.info(msg).endswith(msg)
