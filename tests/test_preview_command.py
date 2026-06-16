"""Tests for tokenpak preview command."""

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

# A preview sends nothing and runs no compressor, so it must not display any
# fabricated savings figure (a "<number>% reduction" / "<number>% saved"-style
# claim). This pattern catches such a fabricated savings percentage.
_FABRICATED_SAVINGS_RE = re.compile(r"\d+(?:\.\d+)?\s*%\s*(?:reduction|saved)", re.IGNORECASE)


class TestPreviewCommand:
    """Test preview command functionality."""

    def test_preview_with_text_input(self):
        """Test preview shows the honest input size and a neutral savings state."""
        result = subprocess.run(
            [sys.executable, "-m", "tokenpak", "preview", "hello world"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "Input:" in result.stdout
        # Savings are not available at preview time (nothing has been sent).
        assert "not available at preview" in result.stdout
        # No fabricated savings percentage/multiplier must be displayed.
        assert not _FABRICATED_SAVINGS_RE.search(result.stdout)

    def test_preview_json_output(self):
        """Test preview JSON output reports a neutral, non-fabricated savings state."""
        result = subprocess.run(
            [sys.executable, "-m", "tokenpak", "preview", "test data", "--json"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        # The input token estimate is honest and present.
        assert "input_tokens" in data
        assert isinstance(data["input_tokens"], int)
        # Savings are explicitly unavailable at preview time, not fabricated.
        assert data["savings_available"] is False
        assert data["output_tokens"] is None
        assert data["saved_tokens"] is None
        assert data["compression_ratio"] is None

    def test_preview_raw_output(self):
        """Test preview raw output is honest about unavailable savings."""
        result = subprocess.run(
            [sys.executable, "-m", "tokenpak", "preview", "test data", "--raw"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "Input:" in result.stdout
        assert "not available at preview" in result.stdout
        assert not _FABRICATED_SAVINGS_RE.search(result.stdout)

    def test_preview_with_file(self):
        """Test preview reading from file."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("This is test content for preview")
            temp_path = f.name

        try:
            result = subprocess.run(
                [sys.executable, "-m", "tokenpak", "preview", "--file", temp_path],
                capture_output=True,
                text=True,
            )
            assert result.returncode == 0
            assert "Input:" in result.stdout
            assert "not available at preview" in result.stdout
        finally:
            Path(temp_path).unlink()

    def test_preview_verbose_output(self):
        """Test preview with verbose output stays honest (no fabricated metrics)."""
        result = subprocess.run(
            [sys.executable, "-m", "tokenpak", "preview", "test", "--verbose"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "Input:" in result.stdout
        assert not _FABRICATED_SAVINGS_RE.search(result.stdout)

    def test_preview_no_input_error(self):
        """Test preview with no input returns error."""
        result = subprocess.run(
            [sys.executable, "-m", "tokenpak", "preview"],
            input="",
            capture_output=True,
            text=True,
        )
        assert result.returncode == 1
        assert "No input provided" in result.stderr or "No input provided" in result.stdout
