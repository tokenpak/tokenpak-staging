"""test_savings_display.py — Tests for enhanced savings display in status and savings commands."""

from unittest.mock import MagicMock, patch

import pytest

from tokenpak.cli.commands import savings, status
from tokenpak.telemetry.pricing import estimate_savings, get_rates


class TestPricingModule:
    """Test the pricing module."""

    def test_get_rates_known_model(self):
        """Test getting rates for a known model."""
        rates = get_rates("claude-sonnet-4-6")
        assert rates["input"] == 3.0
        assert rates["cached"] == 0.30
        assert rates["output"] == 15.0

    def test_get_rates_unknown_model(self):
        """Test getting rates for an unknown model falls back to default."""
        rates = get_rates("unknown-model-xyz")
        assert rates == {"input": 3.0, "cached": 0.30, "output": 15.0}

    def test_get_rates_none(self):
        """Test getting rates with None uses default."""
        rates = get_rates(None)
        assert rates == {"input": 3.0, "cached": 0.30, "output": 15.0}

    def test_estimate_savings_with_compression(self):
        """Test savings calculation with compression."""
        stats = {
            "tokens_raw": 1_000_000,
            "tokens_saved": 50_000,  # 5% compression
            "cache_read_tokens": 100_000,
            "cache_write_tokens": 200_000,
            "model": "claude-sonnet-4-6",
        }
        result = estimate_savings(stats)

        # Compression: 50k tokens at $3/MTok = $0.15
        assert result["compression_cost_saved"] == pytest.approx(0.15, abs=0.01)

        # Cache: 100k tokens * (3.0 - 0.30) / 1M = $0.27
        assert result["cache_cost_saved"] == pytest.approx(0.27, abs=0.01)

        assert result["compression_tokens_saved"] == 50_000
        assert result["cache_tokens_saved"] == 100_000
        assert result["total_tokens_saved"] == 150_000

    def test_estimate_savings_before_after(self):
        """Test before/after cost calculation."""
        stats = {
            "tokens_raw": 1_000_000,
            "tokens_saved": 100_000,  # 10% compression
            "cache_read_tokens": 200_000,
            "cache_write_tokens": 0,
            "model": "claude-opus-4-6",
        }
        result = estimate_savings(stats)

        # Without TokenPak: 1M tokens * $15/MTok = $15.00
        assert result["cost_without_tokenpak"] == pytest.approx(15.00, abs=0.01)

        # With TokenPak:
        # - Compression reduces to 900k tokens
        # - 200k from cache at $1.50, 700k fresh at $15.00
        # = (200k * 1.5 + 700k * 15) / 1M = $10.80
        expected_with = (200_000 * 1.50 + 700_000 * 15.0) / 1_000_000
        assert result["cost_with_tokenpak"] == pytest.approx(expected_with, abs=0.01)

        # Reduction should be positive
        assert result["reduction_percent"] > 0

    def test_estimate_savings_no_compression(self):
        """Test savings with no compression but cache hits."""
        stats = {
            "tokens_raw": 1_000_000,
            "tokens_saved": 0,  # No compression
            "cache_read_tokens": 300_000,  # 30% cache hits
            "cache_write_tokens": 700_000,
            "model": "claude-sonnet-4-6",
        }
        result = estimate_savings(stats)

        assert result["compression_tokens_saved"] == 0
        assert result["cache_tokens_saved"] == 300_000

        # Total savings: 300k * (3.0 - 0.30) / 1M = $0.81
        assert result["total_cost_saved"] == pytest.approx(0.81, abs=0.01)


class TestStatusCommand:
    """Test the status command with savings display."""

    @patch("tokenpak.cli.commands.status._fetch")
    def test_status_with_savings(self, mock_fetch):
        """Test status command includes savings summary."""
        # Mock the /health endpoint
        health_response = {
            "is_degraded": False,
            "uptime_seconds": 15780,  # 4h 23m
            "version": "v1.2.3",
            "compression_ratio_avg": 0.945,
        }

        # Mock the /stats/session endpoint
        session_response = {
            "session_requests": 1342,
            "tokens_raw": 41_955_704,
            "tokens_saved": 2_333_947,
            "cache_read_tokens": 92_520_228,
            "cache_write_tokens": 0,
            "session_total_saved": 256.81,
            "avg_savings_pct": 5.6,
            "errors": 0,
            "model": "claude-sonnet-4-6",
        }

        def fetch_side_effect(url, timeout=5):
            if "/health" in url:
                return health_response
            elif "/stats/session" in url:
                return session_response
            elif "/degradation" in url:
                return {"recent_events": [], "status": "ok"}
            return None

        mock_fetch.side_effect = fetch_side_effect

        # Capture output
        import io
        import sys

        captured_output = io.StringIO()
        sys.stdout = captured_output

        try:
            status.run(proxy_base="http://127.0.0.1:8766")
            output = captured_output.getvalue()
        finally:
            sys.stdout = sys.__stdout__

        # Check that savings section is present
        assert "💰  Session Savings" in output
        assert "Requests:" in output
        assert "1,342" in output
        assert "Input tokens:" in output
        assert "41,955,704" in output
        assert "Tokens saved:" in output
        assert "Est. saved:" in output

    @patch("tokenpak.cli.commands.status._fetch")
    def test_status_proxy_down(self, mock_fetch):
        """Test status gracefully handles when proxy is down."""
        mock_fetch.return_value = None

        import io
        import sys

        captured_output = io.StringIO()
        sys.stdout = captured_output
        sys.stderr = io.StringIO()

        try:
            with pytest.raises(SystemExit):
                status.run(proxy_base="http://127.0.0.1:8766")
            output = captured_output.getvalue()
        finally:
            sys.stdout = sys.__stdout__

        # Check for helpful error message
        assert "unreachable" in output or "Proxy not running" in output.lower()


class TestSavingsCommand:
    """Test the savings analytics helper — now routed through the unified report.

    The old self-invented ``_query_savings`` SQL (``_connect``-based, with
    ``cost_without_tokenpak`` / ``cost_reduction_pct`` fields) is gone. The
    helper now delegates to ``telemetry.unified_savings.savings_report`` and
    returns the two attribution planes SEPARATELY (compression vs cache),
    never summed.
    """

    def test_savings_two_planes_present(self):
        """Helper returns compression (TokenPak) and cache (client) as separate keys."""
        from tokenpak.telemetry.unified_savings import (
            DB_STATE_ATTRIBUTED,
            SavingsPlane,
            UnifiedSavingsReport,
        )

        fake = UnifiedSavingsReport(
            compression_savings=SavingsPlane(label="compression", usd=2.5, credited_to="tokenpak"),
            cache_savings=SavingsPlane(label="cache", usd=9.0, credited_to="client"),
            db_state=DB_STATE_ATTRIBUTED,
            request_count=100,
        )
        with patch("tokenpak.telemetry.unified_savings.savings_report", return_value=fake):
            result = savings._query_savings(period="24h", db_path="/tmp/x.db")

        assert result["compression_savings_usd"] == 2.5
        assert result["cache_savings_usd"] == 9.0
        # Never a single combined figure.
        assert "savings_amount" not in result
        assert "saved" not in result

    def test_savings_no_data(self):
        """No-data → db_state no_data; figures zero, not fabricated."""
        from tokenpak.telemetry.unified_savings import (
            DB_STATE_NO_DATA,
            UnifiedSavingsReport,
        )

        fake = UnifiedSavingsReport(db_state=DB_STATE_NO_DATA)
        with patch("tokenpak.telemetry.unified_savings.savings_report", return_value=fake):
            result = savings._query_savings(period="24h", db_path="/tmp/missing.db")

        assert result["db_state"] == "no_data"
        assert result["compression_savings_usd"] == 0.0
        assert result["cache_savings_usd"] == 0.0

    def test_client_cache_not_credited_to_tokenpak(self):
        """Client-origin cache stays on the cache plane, never compression."""
        from tokenpak.telemetry.unified_savings import (
            DB_STATE_ATTRIBUTED,
            SavingsPlane,
            UnifiedSavingsReport,
        )

        # All value is client cache; TokenPak compression must be $0.
        fake = UnifiedSavingsReport(
            compression_savings=SavingsPlane(label="compression", usd=0.0, credited_to="tokenpak"),
            cache_savings=SavingsPlane(label="cache", usd=42.0, credited_to="client"),
            db_state=DB_STATE_ATTRIBUTED,
        )
        with patch("tokenpak.telemetry.unified_savings.savings_report", return_value=fake):
            result = savings._query_savings(period="24h", db_path="/tmp/x.db")

        assert result["compression_savings_usd"] == 0.0
        assert result["cache_savings_usd"] == 42.0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
