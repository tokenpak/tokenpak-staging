"""Curated, no-network guard for the bundled examples.

Examples can silently rot after their prose truth is repaired: an import gets
renamed, a referenced file is deleted, or a snippet quietly starts needing a
provider key. This guard protects the *curated* examples — the ones an external
tester is told are copy/paste runnable offline — without live providers, network
access, or broad CI.

What it enforces:

* **Path lint** — every example named in the manifest still exists on disk.
* **Coverage / rot lint** — every ``examples/**/*.py`` is classified exactly
  once (offline-runnable *or* excluded-with-a-reason). A new, untriaged example
  fails the build instead of slipping through.
* **No-network import smoke** — each offline-runnable example imports cleanly
  with outbound sockets blocked and provider API keys stripped from the
  environment, proving it needs neither network nor credentials at import time.

Run it directly (hermetic, fast):

    python -m pytest tests/examples -q

The manifest below is the single source of truth for what is covered and what
is intentionally excluded; keep it in sync when adding or removing examples.
"""
from __future__ import annotations

import contextlib
import importlib.util
import os
import pathlib
import socket
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
EXAMPLES_ROOT = REPO_ROOT / "examples"

# ---------------------------------------------------------------------------
# Curated manifest. Paths are POSIX-relative to examples/.
#
# OFFLINE_RUNNABLE: examples that claim offline / copy-paste readiness. They are
# import-smoked with the network disabled and provider keys removed. Adding an
# example here is a promise that it imports with no network and no credentials.
#
# EXCLUDED: examples deliberately outside the offline smoke, each with a reason.
# Excluding an example keeps it out of the smoke but still subjects it to the
# path + coverage lints, so it cannot silently disappear or go untriaged.
# ---------------------------------------------------------------------------
OFFLINE_RUNNABLE = (
    "async_compression/main.py",
    "basic_compression.py",
    "basic_compression/main.py",
    "cache_management/main.py",
    "cache_usage.py",
    "custom_retrieval_backend.py",
    "custom_semantic_scorer.py",
    "error_handling/main.py",
    "metrics_collection.py",
    "multi_turn_compression/main.py",
    "real_world/api_response_compression.py",
    "real_world/db_query_compression.py",
    "real_world/vector_compression.py",
    "streaming_compression.py",
    "streaming_compression/main.py",
    "with_proxy.py",
)

EXCLUDED = {
    "api_server/server.py": (
        "HTTP server example; requires the fastapi+pydantic web stack and is not "
        "a standalone offline script."
    ),
    "claude_integration/main.py": (
        "Anthropic provider integration; a meaningful run needs ANTHROPIC_API_KEY "
        "and live network access."
    ),
    "django_integration/main.py": (
        "Django web-framework integration; not a standalone offline copy/paste script."
    ),
    "fastapi_middleware/app.py": (
        "FastAPI/Starlette middleware; constructs an ASGI app at import and needs "
        "the web stack."
    ),
    "flask_integration/app.py": (
        "Flask integration; requires the flask web framework."
    ),
    "langchain_integration/main.py": (
        "LangChain integration; needs the langchain stack and a live LLM backend."
    ),
    "openai_wrapper/main.py": (
        "OpenAI provider integration; a meaningful run needs OPENAI_API_KEY and "
        "live network access."
    ),
    "performance_benchmarking/main.py": (
        "Timing/throughput benchmark; host-sensitive and out of scope for a fast "
        "no-network guard."
    ),
}

# Provider/credential env vars stripped during the import smoke so nothing can
# silently depend on a key being present.
_PROVIDER_KEY_ENV = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "TOKENPAK_API_KEY",
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
    "COHERE_API_KEY",
    "MISTRAL_API_KEY",
    "HUGGINGFACE_API_KEY",
    "HF_TOKEN",
)


class _NetworkBlocked(AssertionError):
    """Raised if a curated example attempts outbound network I/O at import."""


def _blocked(*_args, **_kwargs):
    raise _NetworkBlocked(
        "tests/examples is a no-network guard: outbound network is disabled. "
        "A curated example must not perform network I/O at import time."
    )


@contextlib.contextmanager
def _no_network_no_keys():
    """Block outbound sockets and remove provider keys for the duration."""
    saved_env = {k: os.environ.pop(k) for k in _PROVIDER_KEY_ENV if k in os.environ}
    orig_connect = socket.socket.connect
    orig_connect_ex = socket.socket.connect_ex
    orig_create_connection = socket.create_connection
    socket.socket.connect = _blocked
    socket.socket.connect_ex = _blocked
    socket.create_connection = _blocked
    try:
        yield
    finally:
        socket.socket.connect = orig_connect
        socket.socket.connect_ex = orig_connect_ex
        socket.create_connection = orig_create_connection
        os.environ.update(saved_env)


def _module_name_for(rel: str) -> str:
    parts = pathlib.PurePosixPath(rel).with_suffix("").parts
    return "tokenpak_curated_example_" + "_".join(parts)


def _discovered_examples() -> set[str]:
    return {
        p.relative_to(EXAMPLES_ROOT).as_posix()
        for p in EXAMPLES_ROOT.rglob("*.py")
    }


@pytest.mark.quick
@pytest.mark.parametrize("rel", sorted(set(OFFLINE_RUNNABLE) | set(EXCLUDED)))
def test_manifest_path_exists(rel: str) -> None:
    """Path lint: every manifest entry still points at a real file."""
    assert (EXAMPLES_ROOT / rel).is_file(), (
        f"examples manifest references a missing example: examples/{rel}"
    )


@pytest.mark.quick
def test_manifest_covers_every_example() -> None:
    """Rot guard: every example is classified exactly once."""
    discovered = _discovered_examples()
    offline = set(OFFLINE_RUNNABLE)
    excluded = set(EXCLUDED)

    overlap = offline & excluded
    assert not overlap, (
        "example(s) listed in both OFFLINE_RUNNABLE and EXCLUDED: " f"{sorted(overlap)}"
    )

    unclassified = discovered - offline - excluded
    assert not unclassified, (
        "new example(s) not classified in the tests/examples manifest — add each "
        "to OFFLINE_RUNNABLE (if it imports offline with no keys) or to EXCLUDED "
        f"with a reason: {sorted(unclassified)}"
    )

    stale = (offline | excluded) - discovered
    assert not stale, (
        f"manifest lists example(s) that no longer exist on disk: {sorted(stale)}"
    )


@pytest.mark.quick
def test_excluded_examples_carry_reasons() -> None:
    """Every exclusion must carry an explicit, human-readable reason."""
    for rel, reason in EXCLUDED.items():
        assert isinstance(reason, str) and len(reason.strip()) >= 12, (
            f"excluded example examples/{rel} needs an explicit reason"
        )


@pytest.mark.quick
@pytest.mark.parametrize("rel", sorted(OFFLINE_RUNNABLE))
def test_offline_example_imports_without_network_or_keys(rel: str) -> None:
    """Import smoke: curated example imports with no network and no provider keys."""
    example_path = EXAMPLES_ROOT / rel
    assert example_path.is_file(), f"curated example missing: examples/{rel}"

    module_name = _module_name_for(rel)
    spec = importlib.util.spec_from_file_location(module_name, example_path)
    assert spec is not None and spec.loader is not None

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        with _no_network_no_keys():
            spec.loader.exec_module(module)
    finally:
        sys.modules.pop(module_name, None)
