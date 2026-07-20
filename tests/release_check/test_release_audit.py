"""Contract tests for the full release audit and umbrella helpers."""

from __future__ import annotations

import dataclasses
import sqlite3
from datetime import date

import pytest

from scripts.fresh_install_demo import FreshInstallError, validate_demo_output
from scripts.release_audit import (
    MYPY_LOGICAL_COMMAND,
    AuditError,
    collect_mypy_evidence,
    parse_added_diff_lines,
    scan_doc_patterns,
    scan_forbidden_lines,
    telemetry_summary,
    validate_accepted_finding,
)


def _mypy_output() -> bytes:
    return (
        b"tokenpak/core/a.py:1: error: Missing return statement  [return]\n"
        b"tokenpak/proxy/b.py:2: error: Function is missing a type annotation  [no-untyped-def]\n"
        b"Found 2 errors in 2 files (checked 9 source files)\n"
    )


def _receipt(evidence):
    return {
        "schema_version": 1,
        "gate": "A3",
        "approval": "APPROVE-A3-V113-ONLY-WAIVER",
        "release_version": "1.13.0",
        "python_version": "3.12",
        "mypy_version": "2.3.0",
        "command": list(MYPY_LOGICAL_COMMAND),
        "expected_exit": 1,
        "error_count": evidence.error_count,
        "unique_error_count": evidence.unique_error_count,
        "file_count": evidence.file_count,
        "checked_file_count": evidence.checked_file_count,
        "in_scope_error_count": evidence.in_scope_error_count,
        "in_scope_file_count": evidence.in_scope_file_count,
        "raw_sha256": evidence.raw_sha256,
        "normalized_sha256": evidence.normalized_sha256,
        "approved_base_commit": "abc123",
        "approved_baseline_normalized_sha256": evidence.normalized_sha256,
        "delta_attribution": "No error-line delta; one additional empty source was checked.",
    }


def test_mypy_receipt_accepts_only_exact_evidence():
    evidence = collect_mypy_evidence(_mypy_output(), 1)
    validate_accepted_finding(
        evidence,
        _receipt(evidence),
        release_version="1.13.0",
        python_version="3.12",
        mypy_version="2.3.0",
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("raw_sha256", "0" * 64),
        ("normalized_sha256", "1" * 64),
        ("error_count", 1),
        ("file_count", 1),
        ("mypy_version", "2.2.0"),
        ("release_version", "1.14.0"),
        ("command", ["python", "-m", "mypy", "tokenpak/core"]),
    ],
)
def test_mypy_receipt_rejects_every_contract_drift(field, value):
    evidence = collect_mypy_evidence(_mypy_output(), 1)
    receipt = _receipt(evidence)
    receipt[field] = value
    with pytest.raises(AuditError, match="receipt mismatch"):
        validate_accepted_finding(
            evidence,
            receipt,
            release_version="1.13.0",
            python_version="3.12",
            mypy_version="2.3.0",
        )


def test_mypy_transcript_must_have_terminal_summary():
    with pytest.raises(AuditError, match="terminal summary"):
        collect_mypy_evidence(b"tokenpak/core/a.py:1: error: boom  [misc]\n", 1)


def test_mypy_normalization_preserves_emission_order():
    output = (
        b"tokenpak/proxy/z.py:2: error: zed  [misc]\n"
        b"tokenpak/core/a.py:1: error: alpha  [misc]\n"
        b"Found 2 errors in 2 files (checked 2 source files)\n"
    )
    first = collect_mypy_evidence(output, 1)
    reversed_output = (
        b"tokenpak/core/a.py:1: error: alpha  [misc]\n"
        b"tokenpak/proxy/z.py:2: error: zed  [misc]\n"
        b"Found 2 errors in 2 files (checked 2 source files)\n"
    )
    second = collect_mypy_evidence(reversed_output, 1)
    assert first.normalized_sha256 != second.normalized_sha256


def test_mypy_summary_count_must_match_transcript():
    output = b"tokenpak/core/a.py:1: error: boom  [misc]\nFound 2 errors in 1 file\n"
    with pytest.raises(AuditError, match="terminal summary"):
        collect_mypy_evidence(output, 1)


def test_forbidden_scanner_distinguishes_qualifier_from_not_just():
    lines = [
        ("docs/a.md", 1, "This is not just a cache."),
        ("docs/a.md", 2, "This isn't just a cache."),
        ("docs/a.md", 3, "Just run the command."),
        ("docs/a.md", 4, "An industry-leading result."),
    ]
    assert scan_forbidden_lines(lines) == [
        "docs/a.md:3: forbidden qualifier 'just'",
        "docs/a.md:4: forbidden qualifier 'industry-leading'",
    ]


def test_release_doc_scanner_rejects_patterns_and_stale_dates():
    lines = [
        ("docs/a.md", 1, "TODO: finish"),
        ("docs/a.md", 2, "Coming soon"),
        ("docs/a.md", 3, "Updated: 2026-01-01"),
    ]
    findings = scan_doc_patterns(lines, base_date=date(2026, 7, 1))
    assert len(findings) == 3


def test_release_doc_diff_parser_scans_only_added_lines():
    diff = """\
diff --git a/docs/a.md b/docs/a.md
index 1234567..89abcde 100644
--- a/docs/a.md
+++ b/docs/a.md
@@ -4 +4 @@
-TODO: legacy finding outside the release change
+Replacement text
@@ -8,0 +9,2 @@
+Coming soon
+Updated: 2026-01-01
"""
    lines = parse_added_diff_lines(diff)
    assert lines == [
        ("docs/a.md", 4, "Replacement text"),
        ("docs/a.md", 9, "Coming soon"),
        ("docs/a.md", 10, "Updated: 2026-01-01"),
    ]
    assert len(scan_doc_patterns(lines, base_date=date(2026, 7, 1))) == 2


def _telemetry_connection(cache_origin="proxy"):
    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE TABLE requests (cache_origin TEXT, attribution_source TEXT, "
        "compressed_tokens INTEGER, cache_read_tokens INTEGER)"
    )
    connection.execute(
        "INSERT INTO requests VALUES (?, 'runtime', 4, 5)",
        (cache_origin,),
    )
    return connection


def test_telemetry_summary_runs_canonical_queries():
    connection = _telemetry_connection()
    try:
        assert telemetry_summary(connection) == {
            "cache_origin_counts": {"proxy": 1},
            "attribution_source_counts": {"runtime": 1},
            "compressed_tokens": 4,
            "proxy_cache_read_tokens": 5,
        }
    finally:
        connection.close()


def test_telemetry_summary_fails_on_null_cache_origin():
    connection = _telemetry_connection(None)
    try:
        with pytest.raises(AuditError, match="NULL cache_origin"):
            telemetry_summary(connection)
    finally:
        connection.close()


def test_telemetry_summary_fails_on_schema_drift():
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE requests (cache_origin TEXT)")
    try:
        with pytest.raises(AuditError, match="schema is missing"):
            telemetry_summary(connection)
    finally:
        connection.close()


def test_demo_output_requires_all_contract_markers():
    validate_demo_output("TokenPak — Offline Fixture Demo\nReceipt status\nnot a savings receipt")
    with pytest.raises(FreshInstallError, match="missing"):
        validate_demo_output("TokenPak — Offline Fixture Demo")


def test_mypy_evidence_is_immutable():
    evidence = collect_mypy_evidence(_mypy_output(), 1)
    with pytest.raises(dataclasses.FrozenInstanceError):
        evidence.error_count = 0
