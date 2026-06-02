"""Regression: the companion MCP server must start without loading the heavy
ML stack (``sentence_transformers`` / ``transformers`` / ``torch``).

Background
----------
Importing ``tokenpak.companion.mcp_server`` transitively reaches
``tokenpak.vault.retrieval.vector_local`` (companion → proxy → router → vault
retrieval). That module used to ``import sentence_transformers`` at module-load
time, which pulls in ``transformers`` + ``torch`` — a ~13s cold import. The
result: the companion MCP server took ~15–20s just to become importable, so it
never answered the MCP ``initialize`` handshake inside Claude Code's
MCP-connect window and Claude Code reported it as a failed setup
("⚠ N setup issues: MCP").

The fix makes ``sentence_transformers`` lazy in ``vector_local`` (detected via
``importlib.util.find_spec`` at import, imported for real only when retrieval
is actually invoked). These tests lock that contract in.

Each test runs in a fresh subprocess so ``sys.modules`` is clean (other tests
in the suite may legitimately import torch) and uses ``-P`` to avoid the
repo-root cwd shadow that otherwise resolves ``tokenpak`` to a bare namespace
package.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys

# The heavy, slow-to-import optional ML dependencies that must NOT be pulled in
# as a side effect of starting the companion MCP server.
HEAVY_MODULES = ("sentence_transformers", "transformers", "torch")

_SUBPROC_TIMEOUT = 120  # generous: a cold torch import (if it regressed) is ~13s


def _run_py(code: str) -> subprocess.CompletedProcess:
    """Run ``code`` in a fresh interpreter (``-P`` = no cwd on sys.path)."""
    return subprocess.run(
        [sys.executable, "-P", "-c", code],
        capture_output=True,
        text=True,
        timeout=_SUBPROC_TIMEOUT,
    )


def _loaded_heavy(stdout: str) -> list[str]:
    line = [ln for ln in stdout.splitlines() if ln.startswith("LOADED:")][-1]
    payload = line[len("LOADED:"):]
    return [m for m in payload.split(",") if m]


def test_companion_mcp_server_import_does_not_load_ml_stack():
    """Importing the MCP server module must not import the heavy ML stack."""
    code = (
        "import sys\n"
        "import tokenpak.companion.mcp_server  # noqa: F401\n"
        f"loaded = [m for m in {HEAVY_MODULES!r} if m in sys.modules]\n"
        "print('LOADED:' + ','.join(loaded))\n"
    )
    proc = _run_py(code)
    assert proc.returncode == 0, f"import raised:\n{proc.stderr}"
    loaded = _loaded_heavy(proc.stdout)
    assert loaded == [], (
        f"companion MCP server import pulled in heavy ML modules {loaded}; "
        "these must stay lazy so the server answers MCP `initialize` promptly "
        "(see tokenpak/vault/retrieval/vector_local.py)."
    )


def test_mcp_server_serve_dispatch_does_not_load_ml_stack():
    """Running the server's init/list/dispatch path stays free of the ML stack.

    Exercises the actual request handlers (``initialize`` + ``tools/list``)
    the way Claude Code drives them at connect time, then asserts none of the
    heavy modules were imported as a side effect.
    """
    code = (
        "import io, sys\n"
        "from tokenpak.companion.mcp_server._impl import serve\n"
        "reqs = (\n"
        '    \'{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\",\"params\":{}}\\n\'\n'
        '    \'{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"tools/list\",\"params\":{}}\\n\'\n'
        ")\n"
        "out = io.StringIO()\n"
        "serve(stdin=io.StringIO(reqs), stdout=out)\n"
        "assert '\"result\"' in out.getvalue(), out.getvalue()[:200]\n"
        f"loaded = [m for m in {HEAVY_MODULES!r} if m in sys.modules]\n"
        "print('LOADED:' + ','.join(loaded))\n"
    )
    proc = _run_py(code)
    assert proc.returncode == 0, f"serve() raised:\n{proc.stderr}"
    loaded = _loaded_heavy(proc.stdout)
    assert loaded == [], (
        f"companion MCP serve() pulled in heavy ML modules {loaded}; the "
        "initialize/tools-list handshake must not touch the embedding backend."
    )


def test_vector_local_import_is_lazy_but_detects_backend():
    """vector_local imports cheaply yet still reports backend availability."""
    code = (
        "import sys\n"
        "import tokenpak.vault.retrieval.vector_local as vl\n"
        "print('ST_AT_IMPORT:' + str('sentence_transformers' in sys.modules))\n"
        "print('AVAILABLE:' + str(vl._ST_AVAILABLE))\n"
    )
    proc = _run_py(code)
    assert proc.returncode == 0, proc.stderr
    assert "ST_AT_IMPORT:False" in proc.stdout, (
        f"sentence_transformers imported at vector_local import time:\n{proc.stdout}"
    )
    # Availability detection must still reflect reality in this environment.
    expected = importlib.util.find_spec("sentence_transformers") is not None
    assert f"AVAILABLE:{expected}" in proc.stdout, proc.stdout


def test_vector_retrieval_loads_backend_on_demand():
    """When retrieval is actually used, the loader imports the backend.

    This is the other half of the lazy-import contract: laziness must not mean
    'never' — invoking ``_load_sentence_transformer()`` must perform the
    ``from sentence_transformers import SentenceTransformer`` and wire the class
    into the module global.

    A lightweight fake ``sentence_transformers`` module is injected so the
    import *wiring* is exercised deterministically, without paying the real
    ~13s ``torch``/``transformers`` cost (which would make the test timing
    flaky in CI). The companion of this test —
    ``test_vector_local_import_is_lazy_but_detects_backend`` — already proves
    the real backend is detected (and not loaded) at import time.
    """
    code = (
        "import sys, types, importlib.machinery\n"
        "fake = types.ModuleType('sentence_transformers')\n"
        "fake.__spec__ = importlib.machinery.ModuleSpec('sentence_transformers', loader=None)\n"
        "class _FakeST:\n"
        "    def __init__(self, *a, **k): pass\n"
        "fake.SentenceTransformer = _FakeST\n"
        "sys.modules['sentence_transformers'] = fake\n"
        "import tokenpak.vault.retrieval.vector_local as vl\n"
        "assert vl.SentenceTransformer is None, 'class bound before first use'\n"
        "cls = vl._load_sentence_transformer()\n"
        "assert cls is _FakeST, cls\n"
        "assert vl.SentenceTransformer is _FakeST, 'module global not populated on demand'\n"
        "print('OK')\n"
    )
    proc = _run_py(code)
    assert proc.returncode == 0, proc.stderr
    assert "OK" in proc.stdout, proc.stdout
