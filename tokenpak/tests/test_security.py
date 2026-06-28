# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for tokenpak.security
"""

from __future__ import annotations

import io
import json
import logging
import os
import stat

import pytest

from tokenpak.security import (
    RedactingLogFilter,
    ensure_config_permissions,
    install_log_redaction,
    redact_pii,
    safe_temp_file,
    sanitize_cli_arg,
    sanitize_model_name,
    secure_write_config,
)

# ---------------------------------------------------------------------------
# secure_write_config
# ---------------------------------------------------------------------------


class TestSecureWriteConfig:
    def test_writes_valid_json(self, tmp_path):
        p = tmp_path / "config.json"
        data = {"key": "value", "num": 42}
        secure_write_config(p, data)
        assert p.exists()
        assert json.loads(p.read_text()) == data

    def test_file_permissions_are_600(self, tmp_path):
        p = tmp_path / "config.json"
        secure_write_config(p, {"x": 1})
        mode = stat.S_IMODE(p.stat().st_mode)
        assert mode == 0o600

    def test_atomic_write_on_existing_file(self, tmp_path):
        p = tmp_path / "config.json"
        secure_write_config(p, {"v": 1})
        secure_write_config(p, {"v": 2})
        assert json.loads(p.read_text()) == {"v": 2}

    def test_raises_if_parent_missing(self, tmp_path):
        p = tmp_path / "nonexistent_dir" / "config.json"
        with pytest.raises(OSError):
            secure_write_config(p, {"x": 1})

    def test_nested_data_serialized(self, tmp_path):
        p = tmp_path / "config.json"
        data = {"a": [1, 2, 3], "b": {"c": True}}
        secure_write_config(p, data)
        assert json.loads(p.read_text()) == data


# ---------------------------------------------------------------------------
# ensure_config_permissions
# ---------------------------------------------------------------------------


class TestEnsureConfigPermissions:
    def test_returns_false_for_missing_file(self, tmp_path):
        assert ensure_config_permissions(tmp_path / "missing.json") is False

    def test_returns_true_when_already_600(self, tmp_path):
        p = tmp_path / "cfg.json"
        p.write_text("{}")
        p.chmod(0o600)
        assert ensure_config_permissions(p) is True
        assert stat.S_IMODE(p.stat().st_mode) == 0o600

    def test_fixes_permissions_to_600(self, tmp_path):
        p = tmp_path / "cfg.json"
        p.write_text("{}")
        p.chmod(0o644)
        result = ensure_config_permissions(p)
        assert result is True
        assert stat.S_IMODE(p.stat().st_mode) == 0o600


# ---------------------------------------------------------------------------
# sanitize_model_name
# ---------------------------------------------------------------------------


class TestSanitizeModelName:
    @pytest.mark.parametrize("name", [
        "gpt-4o",
        "claude-sonnet-4-6",
        "google/gemini-2-flash",
        "model_v1.2",
        "a",
    ])
    def test_valid_names_pass(self, name):
        assert sanitize_model_name(name) == name

    @pytest.mark.parametrize("name", [
        "../etc/passwd",
        "model;rm -rf /",
        "model|cat",
        "model$(evil)",
        "model`cmd`",
        "a" * 257,
        "",
    ])
    def test_invalid_names_raise(self, name):
        with pytest.raises(ValueError):
            sanitize_model_name(name)

    def test_raises_on_non_string(self):
        with pytest.raises(ValueError):
            sanitize_model_name(123)

    def test_blocks_path_traversal(self):
        with pytest.raises(ValueError):
            sanitize_model_name("some/../model")


# ---------------------------------------------------------------------------
# sanitize_cli_arg
# ---------------------------------------------------------------------------


class TestSanitizeCliArg:
    @pytest.mark.parametrize("value", [
        "hello",
        "valid-arg",
        "some_value123",
        "/absolute/path",
    ])
    def test_valid_args_pass(self, value):
        assert sanitize_cli_arg(value) == value

    @pytest.mark.parametrize("value", [
        "../etc/passwd",
        "value;rm -rf /",
        "val|cat /etc",
        "val&&evil",
        "$(whoami)",
        "`id`",
        "<script>alert(1)</script>",
        "javascript:void(0)",
    ])
    def test_injection_patterns_raise(self, value):
        with pytest.raises(ValueError):
            sanitize_cli_arg(value)

    def test_raises_on_non_string(self):
        with pytest.raises(ValueError):
            sanitize_cli_arg(42)

    def test_error_message_includes_name(self):
        with pytest.raises(ValueError, match="my_param"):
            sanitize_cli_arg("bad;input", name="my_param")


# ---------------------------------------------------------------------------
# redact_pii
# ---------------------------------------------------------------------------


class TestRedactPii:
    def test_redacts_sk_key(self):
        result = redact_pii("key=sk-abcdefghijk123")
        assert "sk-abcdefghijk123" not in result
        assert "[REDACTED-SK]" in result

    def test_redacts_bearer_token(self):
        result = redact_pii("Authorization: Bearer mytoken123")
        assert "mytoken123" not in result

    def test_redacts_api_key_json(self):
        result = redact_pii('{"api_key": "supersecret"}')
        assert "supersecret" not in result

    def test_safe_string_unchanged(self):
        text = "Hello world, no secrets here"
        assert redact_pii(text) == text

    def test_redacts_x_tokenpak_key_header(self):
        result = redact_pii("X-TokenPak-Key: secret-val-xyz")
        assert "secret-val-xyz" not in result
        assert "[REDACTED]" in result

    def test_multiple_patterns_in_one_string(self):
        text = "sk-abc12345678 and api_key=mykey"
        result = redact_pii(text)
        assert "sk-abc12345678" not in result
        assert "mykey" not in result

    def test_returns_string(self):
        assert isinstance(redact_pii("test"), str)


# ---------------------------------------------------------------------------
# safe_temp_file
# ---------------------------------------------------------------------------


class TestSafeTempFile:
    def test_returns_fd_and_path(self, tmp_path):
        fd, path = safe_temp_file(dir=tmp_path)
        assert isinstance(fd, int)
        assert isinstance(path, str)
        os.close(fd)
        os.unlink(path)

    def test_file_permissions_are_600(self, tmp_path):
        fd, path = safe_temp_file(dir=tmp_path)
        os.close(fd)
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode == 0o600
        os.unlink(path)

    def test_custom_suffix(self, tmp_path):
        fd, path = safe_temp_file(suffix=".json", dir=tmp_path)
        os.close(fd)
        assert path.endswith(".json")
        os.unlink(path)


# ---------------------------------------------------------------------------
# Structural redaction at the logging sink (d3 spec gap #6)
# v2.0-d3-credential-security §8 / §10 #6 / AC #10
# ---------------------------------------------------------------------------


def _logger_with_capture(name: str):
    """A logger wired to an in-memory StreamHandler. Returns (logger, handler, buf)."""
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger(name)
    logger.handlers = []
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    return logger, handler, buf


class TestRedactionAtSink:
    def test_raw_authorization_header_redacted_without_caller_discipline(self):
        """AC #10 / gap #6: a logger handed a raw Authorization header emits
        ``[REDACTED]`` even though the caller never scrubbed it."""
        logger, handler, buf = _logger_with_capture("tpk.test.sink1")
        install_log_redaction(handler)
        secret = "sk-livesecretABCDEF1234567890"  # gitleaks:allow (synthetic test fixture, not a real key)
        # Caller does NOT pre-scrub — redaction must be structural at the sink.
        logger.warning("upstream call Authorization: Bearer %s", secret)
        out = buf.getvalue()
        assert secret not in out, "raw credential leaked past the sink"
        assert "[REDACTED]" in out

    def test_format_args_path_is_redacted(self):
        """The secret arriving via %-args (not the literal msg) is still caught,
        because the filter redacts the fully-rendered record."""
        logger, handler, buf = _logger_with_capture("tpk.test.sink2")
        install_log_redaction(handler)
        logger.info("token=%s", "sk-anotherSECRET0987654321")  # gitleaks:allow (synthetic test fixture, not a real key)
        out = buf.getvalue()
        assert "sk-anotherSECRET0987654321" not in out  # gitleaks:allow (synthetic test fixture, not a real key)
        assert "[REDACTED-SK]" in out

    def test_safe_message_passes_through_unchanged(self):
        logger, handler, buf = _logger_with_capture("tpk.test.sink3")
        install_log_redaction(handler)
        logger.info("proxy started on port 8766")
        assert buf.getvalue().strip() == "proxy started on port 8766"

    def test_install_is_idempotent(self):
        handler = logging.StreamHandler(io.StringIO())
        install_log_redaction(handler)
        install_log_redaction(handler)
        filters = [f for f in handler.filters if isinstance(f, RedactingLogFilter)]
        assert len(filters) == 1, "second install must not stack a duplicate filter"

    def test_install_on_logger_covers_existing_handlers(self):
        logger, handler, _buf = _logger_with_capture("tpk.test.sink4")
        install_log_redaction(logger)
        assert any(isinstance(f, RedactingLogFilter) for f in logger.filters)
        assert any(isinstance(f, RedactingLogFilter) for f in handler.filters)

    def test_default_target_is_root_logger(self):
        root = logging.getLogger()
        had = [f for f in root.filters if isinstance(f, RedactingLogFilter)]
        try:
            flt = install_log_redaction()
            assert flt in root.filters
        finally:
            if not had:
                root.removeFilter(flt)


def test_configure_logging_installs_redaction_on_handlers():
    """Integration: ``configure_logging`` wires the structural filter onto the
    tokenpak logger's handler, so the gap-#6 guarantee holds in production."""
    from tokenpak.core import logging_config

    saved_handlers = list(logging.getLogger(logging_config.TPK_LOGGER_NAME).handlers)
    saved_configured = logging_config._CONFIGURED
    try:
        logging_config._CONFIGURED = False
        logging.getLogger(logging_config.TPK_LOGGER_NAME).handlers = []
        logging_config.configure_logging(level="DEBUG", fmt="text")
        handlers = logging.getLogger(logging_config.TPK_LOGGER_NAME).handlers
        assert handlers, "configure_logging must attach at least one handler"
        assert all(
            any(isinstance(f, RedactingLogFilter) for f in h.filters) for h in handlers
        ), "every tokenpak handler must carry the structural redaction filter"
    finally:
        logging.getLogger(logging_config.TPK_LOGGER_NAME).handlers = saved_handlers
        logging_config._CONFIGURED = saved_configured
