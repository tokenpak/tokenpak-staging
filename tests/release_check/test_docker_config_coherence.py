"""Keep Docker configuration mounts aligned with the documented runtime path."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PATH = ROOT / "docker-compose.yml"
GUIDE_PATH = ROOT / "docs" / "DOCKER.md"
HOST_CONFIG_PATH = "./config/config.yaml"
CONTAINER_CONFIG_PATH = "/app/config/config.yaml"
LEGACY_CONTAINER_CONFIG_PATH = "/home/tokenpak/.tokenpak/config.yaml"


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
