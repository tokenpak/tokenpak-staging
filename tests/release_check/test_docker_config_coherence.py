"""Keep Docker configuration mounts aligned with the documented runtime path."""

import shlex
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PATH = ROOT / "docker-compose.yml"
GUIDE_PATH = ROOT / "docs" / "DOCKER.md"
HOST_CONFIG_PATH = "./config/config.yaml"
CONTAINER_CONFIG_PATH = "/app/config/config.yaml"
LEGACY_CONTAINER_CONFIG_PATH = "/home/tokenpak/.tokenpak/config.yaml"


def _docker_run_commands(guide: str) -> list[str]:
    """Collect complete docker-run commands, including continued lines."""
    commands = []
    lines = guide.splitlines()
    index = 0

    while index < len(lines):
        line = lines[index].strip()
        if not line.startswith("docker run"):
            index += 1
            continue

        command_lines = [line]
        while command_lines[-1].endswith("\\"):
            index += 1
            assert index < len(lines), "unterminated docker run command"
            command_lines.append(lines[index].strip())

        commands.append(
            " ".join(line.removesuffix("\\").rstrip() for line in command_lines)
        )
        index += 1

    return commands


def _tokenpak_config_assignments(command: str) -> list[str]:
    """Return TOKENPAK_CONFIG assignments attached to Docker env options."""
    tokens = shlex.split(command)
    assignments = []

    for index, token in enumerate(tokens):
        if token in {"-e", "--env"}:
            assert index + 1 < len(tokens), "Docker environment option has no value"
            assignment = tokens[index + 1]
        elif token.startswith("--env="):
            assignment = token.removeprefix("--env=")
        else:
            continue

        if assignment.partition("=")[0] == "TOKENPAK_CONFIG":
            assignments.append(assignment)

    return assignments


def _has_exact_config_binding(command: str) -> bool:
    expected = f"TOKENPAK_CONFIG={CONTAINER_CONFIG_PATH}"
    return _tokenpak_config_assignments(command) == [expected]


def test_docker_config_path_is_coherent():
    compose = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
    service = compose["services"]["tokenpak"]
    expected_mount = f"{HOST_CONFIG_PATH}:{CONTAINER_CONFIG_PATH}:ro"

    assert service["environment"]["TOKENPAK_CONFIG"] == CONTAINER_CONFIG_PATH
    assert service["volumes"].count(expected_mount) == 1

    guide = GUIDE_PATH.read_text(encoding="utf-8")
    assert LEGACY_CONTAINER_CONFIG_PATH not in guide
    assert f"$(pwd)/config/config.yaml:{CONTAINER_CONFIG_PATH}:ro" in guide
    assert f"to {CONTAINER_CONFIG_PATH}" in guide

    config_mount_commands = [
        command for command in _docker_run_commands(guide) if ":/app/config" in command
    ]
    assert len(config_mount_commands) == 3
    assert all(_has_exact_config_binding(command) for command in config_mount_commands)


def test_docker_config_binding_rejects_wrong_or_conflicting_values():
    wrong_value = (
        "docker run -e TOKENPAK_CONFIG=/app/config/config.yaml.bak "
        "-v ./config/config.yaml:/app/config/config.yaml:ro tokenpak"
    )
    conflicting_values = (
        "docker run -e TOKENPAK_CONFIG=/app/config/config.yaml "
        "-e TOKENPAK_CONFIG=/app/config/config.yaml.bak "
        "-v ./config/config.yaml:/app/config/config.yaml:ro tokenpak"
    )

    assert not _has_exact_config_binding(wrong_value)
    assert not _has_exact_config_binding(conflicting_values)
