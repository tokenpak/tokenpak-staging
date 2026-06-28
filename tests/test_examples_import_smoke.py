from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
EXAMPLES_ROOT = REPO_ROOT / "examples"

OPTIONAL_DEPS_BY_EXAMPLE = {
    pathlib.Path("api_server/server.py"): ("fastapi", "pydantic"),
    pathlib.Path("fastapi_middleware/app.py"): ("fastapi", "pydantic", "starlette"),
    pathlib.Path("flask_integration/app.py"): ("flask",),
}


def _module_name_for(path: pathlib.Path) -> str:
    rel = path.relative_to(EXAMPLES_ROOT).with_suffix("")
    return "tokenpak_example_smoke_" + "_".join(rel.parts)


@pytest.mark.parametrize(
    "example_path",
    sorted(EXAMPLES_ROOT.rglob("*.py")),
    ids=lambda path: str(path.relative_to(EXAMPLES_ROOT)),
)
def test_python_example_imports(example_path: pathlib.Path) -> None:
    rel = example_path.relative_to(EXAMPLES_ROOT)
    for dep in OPTIONAL_DEPS_BY_EXAMPLE.get(rel, ()):
        pytest.importorskip(dep)

    module_name = _module_name_for(example_path)
    spec = importlib.util.spec_from_file_location(module_name, example_path)

    assert spec is not None
    assert spec.loader is not None

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
