"""Mechanical contract tests for the risk-selected workflow topology."""

from __future__ import annotations

from pathlib import Path

import yaml

from scripts.ci.classify_changes import CATEGORY_NAMES
from scripts.ci.verify_selected_jobs import JOB_NAMES

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "risk-selected-ci.yml"
MAKEFILE = ROOT / "Makefile"


def _workflow() -> dict:
    return yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def _commands(job: dict) -> str:
    return "\n".join(step.get("run", "") for step in job["steps"])


def test_changes_job_exports_complete_classifier_contract() -> None:
    jobs = _workflow()["jobs"]
    outputs = jobs["changes"]["outputs"]
    expected = {
        "base",
        "head",
        "classifier_ok",
        *CATEGORY_NAMES,
        "shared_core",
        "large_diff",
        "multi_surface",
        "full_conservative",
        *(f"run_{name}" for name in JOB_NAMES),
    }

    assert set(outputs) == expected
    assert all(
        "steps.classify.outputs" in value or "steps.range.outputs" in value
        for value in outputs.values()
    )


def test_manual_dispatch_requires_a_real_ancestor_range_and_forces_full_coverage() -> None:
    workflow = _workflow()
    commands = _commands(workflow["jobs"]["changes"])

    dispatch = workflow["on"]["workflow_dispatch"]
    assert dispatch["inputs"]["base_sha"]["required"] == "true"
    assert 'base="${{ inputs.base_sha }}"' in commands
    assert 'base" == "$head' in commands
    assert "^[0-9a-fA-F]{40}$" in commands
    assert "git merge-base --is-ancestor" in commands
    assert "--force-full-conservative" in commands


def test_pull_request_range_uses_the_feature_heads_merge_base() -> None:
    commands = _commands(_workflow()["jobs"]["changes"])

    assert 'base_tip="${{ github.event.pull_request.base.sha }}"' in commands
    assert 'base="$(git merge-base "$base_tip" "$head")"' in commands


def test_trust_baseline_is_always_on_and_runs_canonical_baselines() -> None:
    job = _workflow()["jobs"]["trust_baseline"]
    commands = _commands(job)

    assert job["needs"] == "changes"
    assert job["if"] == "${{ always() }}"
    assert 'python -m pip install -e ".[dev]"' in commands
    assert "scripts/ci/trust_baseline.py" in commands
    assert "scripts/release_check/release_check.py" in commands
    assert "public_safety_scan.py" in commands
    assert "license_policy_scan.py" in commands
    assert "gitleaks detect" in commands


def test_every_selectable_job_uses_a_job_level_condition() -> None:
    jobs = _workflow()["jobs"]
    for name in JOB_NAMES:
        job = jobs[name]
        assert job["needs"] == "changes"
        assert job["if"] == ("${{ always() && needs.changes.outputs.run_" + name + " == 'true' }}")


def test_aggregator_depends_on_and_verifies_every_job() -> None:
    job = _workflow()["jobs"]["ci_required"]
    assert job["if"] == "${{ always() }}"
    assert set(job["needs"]) == {"changes", "trust_baseline", *JOB_NAMES}

    env = job["steps"][-1]["env"]
    for name in JOB_NAMES:
        key = name.upper()
        assert env[f"SELECTED_{key}"] == f"${{{{ needs.changes.outputs.run_{name} }}}}"
        assert env[f"RESULT_{key}"] == f"${{{{ needs.{name}.result }}}}"
    assert job["steps"][-1]["run"] == "python3 scripts/ci/verify_selected_jobs.py"


def test_javascript_and_workflow_jobs_carry_the_required_commands() -> None:
    jobs = _workflow()["jobs"]
    javascript_job = jobs["javascript_sdk"]
    javascript = _commands(javascript_job)
    workflow = _commands(jobs["workflow_release"])

    for command in ("npm ci", "npm run build", "npm test", "npm audit --omit=dev"):
        assert command in javascript
    matrix = javascript_job["strategy"]["matrix"]["include"]
    assert {entry["package-directory"] for entry in matrix} == {
        "sdk",
        "packages/tokenpak-js",
    }
    assert javascript_job["defaults"]["run"]["working-directory"] == (
        "${{ matrix.package-directory }}"
    )
    assert any(
        step.get("uses", "").startswith("actions/setup-go@")
        for step in jobs["workflow_release"]["steps"]
    )
    for command in ("actionlint", "check-action-pins.sh", "tests/ci/"):
        assert command in workflow


def test_python_proxy_job_uses_current_partitions_not_legacy_trees() -> None:
    commands = _commands(_workflow()["jobs"]["python_proxy"])

    assert "tokenpak/tests/ tests/proxy/" not in commands
    assert "tests/_internal/" not in commands
    for path in (
        "tokenpak/tests/test_custom_providers.py",
        "tokenpak/tests/test_proxy_config.py",
        "tests/proxy/",
        "tests/config/",
        "tests/services/",
    ):
        assert path in commands


def test_full_and_long_running_python_jobs_partition_all_applicable_tests() -> None:
    jobs = _workflow()["jobs"]
    full_python = _commands(jobs["full_python"])
    long_running = _commands(jobs["integration_chaos"])

    assert "not integration and not chaos and not slow and not needs_fast_host" in full_python
    assert "tests/" in long_running
    assert "(integration or chaos or slow) and not needs_fast_host" in long_running
    assert "tests/integrations/" not in long_running
    assert "tests/chaos/" not in long_running


def test_release_umbrella_quarantines_only_runner_sensitive_timing() -> None:
    source = MAKEFILE.read_text(encoding="utf-8")
    recipe = source.split("test-release-applicable:", 1)[1].split("\n\n", 1)[0]
    release_rule = next(line for line in source.splitlines() if line.startswith("release-check:"))

    assert "RELEASE_APPLICABLE_MARKERS := not needs_fast_host" in source
    assert 'tests/ -m "$(RELEASE_APPLICABLE_MARKERS)"' in recipe
    for retained_partition in ("integration", "chaos", "slow"):
        assert f"not {retained_partition}" not in recipe
    assert "test-release-applicable" in release_rule
    assert " format-check test " not in release_rule
