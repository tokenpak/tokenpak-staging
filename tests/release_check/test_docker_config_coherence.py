"""Keep Docker configuration mounts aligned with the documented runtime path."""

import shlex
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PATH = ROOT / "docker-compose.yml"
GUIDE_PATH = ROOT / "docs" / "DOCKER.md"
HOST_CONFIG_PATH = "./config/config.yaml"
CONTAINER_CONFIG_PATH = "/app/config/config.yaml"
CONTAINER_CONFIG_DIRECTORY = "/app/config"
LEGACY_CONTAINER_CONFIG_PATH = "/home/tokenpak/.tokenpak/config.yaml"
DOCKER_OPTIONS_WITH_VALUES = {
    "-e",
    "--env",
    "-m",
    "--memory",
    "--name",
    "-p",
    "--publish",
    "-v",
    "--volume",
}


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

        commands.append(" ".join(line.removesuffix("\\").rstrip() for line in command_lines))
        index += 1

    return commands


def _docker_run_options(command: str) -> list[str] | None:
    """Return options for a complete, supported Docker run command."""
    tokens = shlex.split(command)
    if tokens[:2] != ["docker", "run"]:
        return None

    options = []
    index = 2

    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            index += 1
            if index != len(tokens) - 1 or tokens[index].startswith("-"):
                return None
            return options
        if not token.startswith("-"):
            return options if index == len(tokens) - 1 else None

        option, separator, value = token.partition("=")
        if separator:
            if option not in DOCKER_OPTIONS_WITH_VALUES or not value:
                return None
            options.append(token)
            index += 1
            continue

        if token not in DOCKER_OPTIONS_WITH_VALUES or index + 1 >= len(tokens):
            return None
        options.extend((token, tokens[index + 1]))
        index += 2

    return None


def _tokenpak_config_assignments(command: str) -> list[str]:
    """Return TOKENPAK_CONFIG assignments attached to Docker env options."""
    options = _docker_run_options(command)
    if options is None:
        return []

    assignments = []

    for index, token in enumerate(options):
        if token in {"-e", "--env"}:
            assert index + 1 < len(options), "Docker environment option has no value"
            assignment = options[index + 1]
        elif token.startswith("--env="):
            assignment = token.removeprefix("--env=")
        else:
            continue

        if assignment.partition("=")[0] == "TOKENPAK_CONFIG":
            assignments.append(assignment)

    return assignments


def _docker_volume_mounts(command: str) -> list[str]:
    """Return volume mounts attached to Docker options before the image."""
    options = _docker_run_options(command)
    if options is None:
        return []

    mounts = []

    for index, token in enumerate(options):
        if token in {"-v", "--volume"}:
            assert index + 1 < len(options), "Docker volume option has no value"
            mounts.append(options[index + 1])
        elif token.startswith("--volume="):
            mounts.append(token.removeprefix("--volume="))

    return mounts


def _has_config_mount(command: str) -> bool:
    """Return whether a Docker option mounts the exact config file or directory."""
    expected_targets = {CONTAINER_CONFIG_PATH, CONTAINER_CONFIG_DIRECTORY}

    for mount in _docker_volume_mounts(command):
        fields = mount.split(":")
        assert len(fields) in {2, 3}, f"unsupported Docker volume form: {mount}"
        if fields[1] in expected_targets:
            return True

    return False


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
        command for command in _docker_run_commands(guide) if _has_config_mount(command)
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
    environment_after_image = (
        "docker run -v ./config/config.yaml:/app/config/config.yaml:ro tokenpak "
        "-e TOKENPAK_CONFIG=/app/config/config.yaml"
    )
    volume_after_image = (
        "docker run -e TOKENPAK_CONFIG=/app/config/config.yaml tokenpak "
        "-v ./config/config.yaml:/app/config/config.yaml:ro"
    )
    wrong_mount_target = (
        "docker run -e TOKENPAK_CONFIG=/app/config/config.yaml "
        "-v ./config/config.yaml:/app/config/config.yaml.bak:ro tokenpak"
    )
    valid_memory_option = (
        "docker run -e TOKENPAK_CONFIG=/app/config/config.yaml "
        "-v ./config/config.yaml:/app/config/config.yaml:ro -m 512m tokenpak"
    )
    memory_without_image = (
        "docker run -e TOKENPAK_CONFIG=/app/config/config.yaml "
        "-v ./config/config.yaml:/app/config/config.yaml:ro -m 512m"
    )
    name_without_image = (
        "docker run -e TOKENPAK_CONFIG=/app/config/config.yaml "
        "-v ./config/config.yaml:/app/config/config.yaml:ro --name tokenpak-demo"
    )
    unknown_option_without_image = (
        "docker run -e TOKENPAK_CONFIG=/app/config/config.yaml "
        "-v ./config/config.yaml:/app/config/config.yaml:ro --future-option value"
    )
    trailing_environment_after_valid_options = (
        "docker run -e TOKENPAK_CONFIG=/app/config/config.yaml "
        "-v ./config/config.yaml:/app/config/config.yaml:ro tokenpak "
        "-e TOKENPAK_CONFIG=/app/config/config.yaml"
    )

    assert not _has_exact_config_binding(wrong_value)
    assert not _has_exact_config_binding(conflicting_values)
    assert not _has_exact_config_binding(environment_after_image)
    assert not _docker_volume_mounts(volume_after_image)
    assert not _has_config_mount(wrong_mount_target)
    assert _has_exact_config_binding(valid_memory_option)
    assert _has_config_mount(valid_memory_option)
    assert not _has_exact_config_binding(memory_without_image)
    assert not _has_config_mount(memory_without_image)
    assert not _has_exact_config_binding(name_without_image)
    assert not _has_config_mount(name_without_image)
    assert not _has_exact_config_binding(unknown_option_without_image)
    assert not _has_config_mount(unknown_option_without_image)
    assert not _has_exact_config_binding(trailing_environment_after_valid_options)
