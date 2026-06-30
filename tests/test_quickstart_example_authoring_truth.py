from __future__ import annotations

import importlib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

PUBLIC_AUTHORING_DOCS = [
    REPO_ROOT / "README.md",
    REPO_ROOT / "docs" / "quickstart.md",
    REPO_ROOT / "docs" / "getting-started.md",
    REPO_ROOT / "docs" / "install-guide.md",
    REPO_ROOT / "docs" / "compression.md",
    REPO_ROOT / "docs" / "cache.md",
    REPO_ROOT / "docs" / "DEPLOYMENT.md",
    REPO_ROOT / "docs" / "API_REFERENCE.md",
    REPO_ROOT / "examples" / "README.md",
    REPO_ROOT / "examples" / "basic_compression.py",
    REPO_ROOT / "examples" / "basic_compression" / "README.md",
]

RETIRED_SNIPPETS = [
    "tokenpak[ml]",
    "tokenpak[tiktoken]",
    "tokenpak[ml,tiktoken]",
    "tokenpak[tiktoken,postgres,redis]",
    "tokenpak[postgres]",
    "tokenpak[redis]",
    "tokenpak.engines",
    "tokenpak.pack",
    "TokenPakClient",
]


def test_public_authoring_docs_do_not_reference_retired_examples() -> None:
    offenders: list[str] = []
    for path in PUBLIC_AUTHORING_DOCS:
        text = path.read_text(encoding="utf-8")
        for snippet in RETIRED_SNIPPETS:
            if snippet in text:
                offenders.append(f"{path.relative_to(REPO_ROOT)} contains {snippet!r}")

    assert offenders == []


def test_documented_sdk_import_paths_exist() -> None:
    for module_name in [
        "tokenpak.compression.engines.base",
        "tokenpak.compression.engines.heuristic",
        "tokenpak.compression.pack",
    ]:
        importlib.import_module(module_name)

    for module_path in [
        REPO_ROOT / "tokenpak" / "proxy" / "adapters" / "anthropic_adapter.py",
        REPO_ROOT / "tokenpak" / "proxy" / "adapters" / "openai_chat_adapter.py",
    ]:
        assert module_path.is_file()
