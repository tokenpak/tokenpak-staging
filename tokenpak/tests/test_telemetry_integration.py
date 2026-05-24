"""Tests for telemetry collection and reporting.

Covers: telemetry/collector.py — TelemetryCollector initialization, file processing, batching.
"""

import json
import tempfile
import time
from pathlib import Path

import pytest

from tokenpak.telemetry.collector import CollectorConfig, TelemetryCollector


class TestTelemetryCollectorBasics:
    """Test: TelemetryCollector initialization and basic configuration."""

    def test_collector_initialization(self):
        """TelemetryCollector initializes with a CollectorConfig."""
        config = CollectorConfig()
        collector = TelemetryCollector(config)
        assert collector is not None
        assert collector.config is config

    def test_collector_default_config(self):
        """CollectorConfig has sensible defaults."""
        config = CollectorConfig()
        assert config.batch_size == 10
        assert config.batch_timeout_seconds == 5.0
        assert config.backfill_on_start is False
        assert "*.jsonl" in config.file_patterns

    def test_collector_custom_config(self):
        """TelemetryCollector accepts custom config values."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = CollectorConfig(
                watch_paths=[Path(tmpdir)],
                batch_size=5,
                batch_timeout_seconds=2.0,
            )
            collector = TelemetryCollector(config)
            assert collector.config.batch_size == 5
            assert collector.config.batch_timeout_seconds == 2.0


class TestFileProcessing:
    """Test: File reading and event extraction from JSONL files."""

    def test_process_jsonl_file(self):
        """Collector reads events from a JSONL file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Write a JSONL file with events
            jsonl_path = Path(tmpdir) / "events.jsonl"
            events = [
                {"type": "completion", "model": "claude-sonnet-4-6", "tokens": 100},
                {"type": "completion", "model": "gpt-4-turbo", "tokens": 200},
            ]
            with open(jsonl_path, "w") as f:
                for evt in events:
                    f.write(json.dumps(evt) + "\n")

            config = CollectorConfig(watch_paths=[Path(tmpdir)])
            collector = TelemetryCollector(config)
            collector._process_file(jsonl_path)

            assert len(collector.pending_events) == 2
            assert collector.pending_events[0]["type"] == "completion"
            assert collector.pending_events[1]["tokens"] == 200

    def test_process_file_tracks_position(self):
        """Collector tracks file position to avoid re-reading."""
        with tempfile.TemporaryDirectory() as tmpdir:
            jsonl_path = Path(tmpdir) / "events.jsonl"
            with open(jsonl_path, "w") as f:
                f.write(json.dumps({"type": "completion", "tokens": 100}) + "\n")

            config = CollectorConfig(watch_paths=[Path(tmpdir)])
            collector = TelemetryCollector(config)
            collector._process_file(jsonl_path)
            assert len(collector.pending_events) == 1

            # Append more data and process again
            with open(jsonl_path, "a") as f:
                f.write(json.dumps({"type": "completion", "tokens": 200}) + "\n")
            collector._process_file(jsonl_path)

            # Should only have added the new event
            assert len(collector.pending_events) == 2

    def test_process_file_skips_invalid_json(self):
        """Collector skips lines that are not valid JSON."""
        with tempfile.TemporaryDirectory() as tmpdir:
            jsonl_path = Path(tmpdir) / "events.jsonl"
            with open(jsonl_path, "w") as f:
                f.write(json.dumps({"type": "completion", "tokens": 100}) + "\n")
                f.write("this is not json\n")
                f.write(json.dumps({"type": "error", "code": 429}) + "\n")

            config = CollectorConfig(watch_paths=[Path(tmpdir)])
            collector = TelemetryCollector(config)
            collector._process_file(jsonl_path)

            # Should have 2 valid events, skipping the bad line
            assert len(collector.pending_events) == 2

    def test_process_file_skips_blank_lines(self):
        """Collector skips blank lines in JSONL files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            jsonl_path = Path(tmpdir) / "events.jsonl"
            with open(jsonl_path, "w") as f:
                f.write(json.dumps({"type": "completion"}) + "\n")
                f.write("\n")
                f.write("   \n")
                f.write(json.dumps({"type": "error"}) + "\n")

            config = CollectorConfig(watch_paths=[Path(tmpdir)])
            collector = TelemetryCollector(config)
            collector._process_file(jsonl_path)

            assert len(collector.pending_events) == 2


class TestBackfill:
    """Test: Backfill reads all existing files on startup."""

    def test_backfill_reads_existing_files(self):
        """Backfill processes all matching files in watch paths."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create two JSONL files
            for name in ["a.jsonl", "b.jsonl"]:
                path = Path(tmpdir) / name
                with open(path, "w") as f:
                    f.write(json.dumps({"type": "completion", "source": name}) + "\n")

            config = CollectorConfig(watch_paths=[Path(tmpdir)])
            collector = TelemetryCollector(config)
            # Monkeypatch _flush_batch to avoid HTTP calls
            collector._flush_batch = lambda force=False: None
            collector.backfill()

            assert len(collector.pending_events) == 2

    def test_backfill_ignores_non_matching_files(self):
        """Backfill skips files that don't match file_patterns."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a matching and a non-matching file
            matching = Path(tmpdir) / "events.jsonl"
            with open(matching, "w") as f:
                f.write(json.dumps({"type": "completion"}) + "\n")

            non_matching = Path(tmpdir) / "readme.txt"
            with open(non_matching, "w") as f:
                f.write("not a jsonl file\n")

            config = CollectorConfig(watch_paths=[Path(tmpdir)])
            collector = TelemetryCollector(config)
            collector._flush_batch = lambda force=False: None
            collector.backfill()

            assert len(collector.pending_events) == 1


class TestStateManagement:
    """Test: State file persistence for crash recovery."""

    def test_state_file_save_and_load(self):
        """Collector saves and loads file state across restarts."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "state.json"
            jsonl_path = Path(tmpdir) / "events.jsonl"
            with open(jsonl_path, "w") as f:
                f.write(json.dumps({"type": "completion"}) + "\n")

            # First collector processes the file
            config1 = CollectorConfig(
                watch_paths=[Path(tmpdir)],
                state_file=state_file,
            )
            collector1 = TelemetryCollector(config1)
            collector1._process_file(jsonl_path)
            collector1._save_state()

            assert state_file.exists()

            # Second collector loads the state
            config2 = CollectorConfig(
                watch_paths=[Path(tmpdir)],
                state_file=state_file,
            )
            collector2 = TelemetryCollector(config2)

            # Should have loaded file states from the state file
            assert len(collector2.file_states) > 0


class TestEdgeCases:
    """Test: Edge cases in telemetry handling."""

    def test_empty_file(self):
        """Collector handles empty files gracefully."""
        with tempfile.TemporaryDirectory() as tmpdir:
            jsonl_path = Path(tmpdir) / "empty.jsonl"
            jsonl_path.touch()

            config = CollectorConfig(watch_paths=[Path(tmpdir)])
            collector = TelemetryCollector(config)
            collector._process_file(jsonl_path)

            assert len(collector.pending_events) == 0

    def test_nonexistent_watch_path(self):
        """Collector handles nonexistent watch paths without crashing."""
        config = CollectorConfig(
            watch_paths=[Path("/nonexistent/path/that/does/not/exist")],
        )
        collector = TelemetryCollector(config)
        collector._flush_batch = lambda force=False: None
        # Backfill should not crash on missing paths
        collector.backfill()
        assert len(collector.pending_events) == 0

    def test_many_events_performance(self):
        """Processing many events from a file doesn't degrade performance."""
        with tempfile.TemporaryDirectory() as tmpdir:
            jsonl_path = Path(tmpdir) / "large.jsonl"
            with open(jsonl_path, "w") as f:
                for i in range(100):
                    f.write(json.dumps({"type": "completion", "tokens": i}) + "\n")

            config = CollectorConfig(watch_paths=[Path(tmpdir)])
            collector = TelemetryCollector(config)

            start = time.time()
            collector._process_file(jsonl_path)
            elapsed = time.time() - start

            assert elapsed < 1.0
            assert len(collector.pending_events) == 100
