"""P8 §3.4 — PAK retrieval/injection overhead.

Isolates the byte-splice cost of injecting a canonical PAK (a 4KB system-prompt
block) into the outbound request body: JSON parse of the 1KB ``messages`` body,
composition of the injected ``system`` block, and re-serialisation. Reported as
the difference versus the same parse/serialise round-trip without injection, so
only the injection-added cost remains.

Per methodology §3.4, semantic-cache PAK *retrieval* (scoring/ranking) is a
separate measurement layered on top of this and is intentionally out of scope
here; this target measures the body byte-splice the proxy performs on every
injected request.

Opt-in: ``pytest -m p8_latency``.
"""

from __future__ import annotations

import json

import pytest

from .p8_latency_harness import requires_p8_optin, run_difference_target

pytestmark = pytest.mark.p8_latency


def _canonical_body_obj() -> dict:
    content = "Summarise the attached context. " * 28  # ~1KB serialised
    return {
        "model": "claude-3-5-sonnet",
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": content}],
    }


def _pak_system_prompt() -> str:
    # ~4KB canonical PAK system-prompt injection.
    return "You are operating with retrieved project context. " * 80


def test_pak_inject_overhead(request):
    requires_p8_optin(request)

    body_bytes = json.dumps(_canonical_body_obj()).encode("utf-8")
    pak = _pak_system_prompt()

    def target() -> None:
        obj = json.loads(body_bytes)
        # Byte-splice the PAK into the outbound body as an injected system block.
        obj["system"] = pak
        _ = json.dumps(obj).encode("utf-8")

    def baseline() -> None:
        obj = json.loads(body_bytes)
        _ = json.dumps(obj).encode("utf-8")

    record = run_difference_target(
        target="pak_inject",
        method=(
            "real JSON body byte-splice: parse 1KB messages body + inject 4KB "
            "system PAK + re-serialise, vs parse/serialise round-trip without "
            "injection (methodology §3.4 body-splice; semantic retrieval deferred)"
        ),
        target_fn=target,
        baseline_fn=baseline,
    )

    assert record["target"] == "pak_inject"
    assert record["sample_size"] > 0
    assert record["status"] == "measured"
