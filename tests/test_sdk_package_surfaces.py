"""Regression tests for SDK/package import and endpoint surfaces."""

from __future__ import annotations

import contextlib
import importlib
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@contextlib.contextmanager
def _prepend_sys_path(*paths: Path):
    entries = [str(path) for path in paths]
    original = list(sys.path)
    sys.path[:0] = entries
    try:
        yield
    finally:
        sys.path[:] = original


def test_sidecar_packages_import_from_source_tree() -> None:
    with _prepend_sys_path(
        ROOT / "packages" / "crewai-tokenpak",
        ROOT / "packages" / "tokenpak-agents",
        ROOT / "packages" / "tokenpak-local",
        ROOT,
    ):
        for module in ("crewai_tokenpak", "tokenpak_agents", "tokenpak_local"):
            importlib.import_module(module)


def test_vendored_sdk_examples_import_without_sidecar_package_names() -> None:
    modules = (
        "tokenpak.sdk.autogen.examples.basic_usage",
        "tokenpak.sdk.crewai.examples.basic_usage",
        "tokenpak.sdk.langchain.examples.basic_rag",
        "tokenpak.sdk.llamaindex.examples.basic_usage",
        "tokenpak.sdk.local.examples.basic_usage",
    )
    with contextlib.redirect_stdout(io.StringIO()):
        for module in modules:
            importlib.import_module(module)


def test_vendored_sdk_surface_no_longer_imports_dead_sidecar_names() -> None:
    dead_imports = (
        "from autogen_tokenpak",
        "from crewai_tokenpak",
        "from langchain_tokenpak",
        "from llamaindex_tokenpak",
        "from tokenpak_local",
        "import autogen_tokenpak",
        "import crewai_tokenpak",
        "import langchain_tokenpak",
        "import llamaindex_tokenpak",
        "import tokenpak_local",
        "pip install tokenpak-local",
    )
    scanned = []
    for path in (ROOT / "tokenpak" / "sdk").rglob("*"):
        if path.suffix not in {".py", ".md"}:
            continue
        scanned.append(path)
        text = path.read_text(encoding="utf-8")
        assert not any(pattern in text for pattern in dead_imports), path
    assert scanned
