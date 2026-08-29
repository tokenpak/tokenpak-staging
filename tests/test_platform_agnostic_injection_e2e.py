# SPDX-License-Identifier: Apache-2.0
"""Ground truth for the json_inject route: does declared injection actually run?

``tests/test_injection_ground_truth_e2e.py`` proves the claim for the
byte-preserved (Claude Code) route by recording the exact bytes a mock
upstream receives. Route policy has declared ``"vault_injection":
"json_inject"`` for the OpenClaw and SDK routes for just as long, and until
now nothing on that path ever invoked the stage: the only pipeline call site
lived inside the byte-preserved branch, so automatic context reached one
client and silently skipped every other route whose policy asked for it.

This suite is that same ground-truth standard applied to the other branch —
the request never carries ``X-Claude-Code-Session-Id``, which is what routes
it onto the json_inject / default policy instead of byte-preserved. Retrieval
is stubbed; everything downstream (route classification, the stage, the real
``ProxyServer``, and the real session accounting ``tokenpak status`` reads) is
real.

It also guards the specific regression this branch's completion produced: the
first cut of the json_inject call site invoked ``_record_injection_in_session``
directly, using the pre-receipt-surfacing two-argument shape, in addition to
the one centralized, correctly-shaped call already made under the session lock
for every request. That extra call raised on every injecting request (silently
swallowed by the enclosing fail-open ``except``) and, being otherwise
inert, would have gone unnoticed by a test that only checked the final
session totals. ``test_the_session_write_happens_exactly_once`` asserts the
real ``ProxyServer.session`` counters directly — a reintroduced duplicate
call double-counts ``injection_hits``/``injected_tokens`` for one request,
so it fails loudly instead of by accident.
"""

from __future__ import annotations

import importlib
import json
import os
import socket
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

pytestmark = pytest.mark.needs_proxy

from tests.proxy._proxy_subprocess import free_port
from tokenpak.proxy.server import ProxyServer

# The string retrieval will "find". If injection works, the provider sees it.
# Distinct from the byte-preserved suite's marker so a copy/paste mistake that
# runs both suites against the same upstream can't cross-contaminate a result.
VAULT_MARKER = "VAULT_JSON_INJECT_MARKER_9d21fe"

# Eligibility floor is ~1000 prompt tokens (vault.inject_min_prompt_tokens), so
# the request must be genuinely large or the stage correctly skips it and the
# test would prove nothing.
_FILLER = "the quick brown fox jumps over the lazy dog. " * 400

_UPSTREAM_RESPONSE = json.dumps(
    {
        "id": "msg_json_inject_ground_truth",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": "ok"}],
        "model": "claude-sonnet-4-6",
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 10, "output_tokens": 2},
    }
).encode()


def _wait_for_port(port: int, host: str = "127.0.0.1", timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            socket.create_connection((host, port), timeout=0.5).close()
            return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"port {host}:{port} did not open within {timeout}s")


class _RecordingUpstream(BaseHTTPRequestHandler):
    """Mock provider that records the exact body it received."""

    received_bodies: list[bytes] = []

    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        type(self).received_bodies.append(self.rfile.read(length))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(_UPSTREAM_RESPONSE)))
        self.end_headers()
        self.wfile.write(_UPSTREAM_RESPONSE)


@pytest.fixture
def upstream():
    _RecordingUpstream.received_bodies = []
    server = HTTPServer(("127.0.0.1", 0), _RecordingUpstream)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port}", _RecordingUpstream
    server.shutdown()


def _reload_vault_config_with_switch(value: bool) -> str | None:
    """Flip ``VAULT_INJECTION_ENABLED`` through its real env-var contract.

    ``tokenpak.proxy.config`` resolves the switch once, at import time, from
    ``TOKENPAK_VAULT_INJECTION``. Setting the env var and reloading the
    module exercises that same resolution path, rather than overwriting the
    already-resolved attribute directly. Returns the prior env value so the
    caller can restore it.
    """
    from tokenpak.proxy import config as cfg_mod

    previous = os.environ.get("TOKENPAK_VAULT_INJECTION")
    os.environ["TOKENPAK_VAULT_INJECTION"] = "1" if value else "0"
    importlib.reload(cfg_mod)
    return previous


def _restore_vault_config(previous: str | None) -> None:
    from tokenpak.proxy import config as cfg_mod

    if previous is None:
        os.environ.pop("TOKENPAK_VAULT_INJECTION", None)
    else:
        os.environ["TOKENPAK_VAULT_INJECTION"] = previous
    importlib.reload(cfg_mod)


@pytest.fixture(autouse=True)
def injection_switch_on():
    """Enable the vault-injection master switch for this suite.

    ``VAULT_INJECTION_ENABLED`` defaults OFF and guards the top of
    ``stage_vault_injection``. Tests that need the OFF behavior override this
    back explicitly (see ``test_policy_off_switch...``) rather than relying
    on the untouched default, so the intent at each call site is visible
    without cross-referencing this fixture. The switch is flipped through its
    real env-var contract and reloaded, not by overwriting the resolved
    attribute in place.
    """
    previous = _reload_vault_config_with_switch(True)
    try:
        yield
    finally:
        _restore_vault_config(previous)


@pytest.fixture
def vault_with_content(monkeypatch):
    """Stub retrieval only. Adapter, json-inject application, and server stay real."""
    import tokenpak.proxy.vault_bridge as vb

    class _Idx:
        available = True

        def compile_injection(self, *a, **kw):
            # (injection_text, tokens_used, source_refs)
            return (VAULT_MARKER, 219, ["decisions/routing.md"])

        def search(self, *a, **kw):
            return []

    monkeypatch.setattr(vb, "get_vault_index", lambda *a, **kw: _Idx(), raising=False)
    return _Idx


@pytest.fixture
def vault_raises(monkeypatch):
    """Stub retrieval to fail inside ``compile_injection``.

    That call is not wrapped in a try/except anywhere between it and the
    ``except Exception: pass`` around the json_inject call site in
    ``_proxy_to_inner`` — so the fail-open guarantee for this route rests
    entirely on that one wrapper. This fixture exercises exactly that seam.
    """
    import tokenpak.proxy.vault_bridge as vb

    class _ExplodingIdx:
        available = True

        def compile_injection(self, *a, **kw):
            raise RuntimeError("synthetic vault failure")

        def search(self, *a, **kw):
            raise RuntimeError("synthetic vault failure")

    monkeypatch.setattr(vb, "get_vault_index", lambda *a, **kw: _ExplodingIdx(), raising=False)
    return _ExplodingIdx


@pytest.fixture
def intercepted_localhost(monkeypatch):
    """Make the mock upstream look like a provider worth intercepting.

    See ``tests/test_injection_ground_truth_e2e.py`` for the full rationale —
    ``server.py`` binds ``INTERCEPT_HOSTS`` by value at import time, so both
    bindings must be patched.
    """
    from tokenpak.proxy import router, server

    patched = set(router.INTERCEPT_HOSTS) | {"127.0.0.1"}

    monkeypatch.setattr(server, "INTERCEPT_HOSTS", patched, raising=False)
    monkeypatch.setattr(router, "INTERCEPT_HOSTS", patched, raising=False)
    try:
        from tokenpak.proxy import fallback

        monkeypatch.setattr(
            fallback,
            "INTERCEPT_HOSTS",
            set(fallback.INTERCEPT_HOSTS) | {"127.0.0.1"},
            raising=False,
        )
    except Exception:
        pass
    return patched


@pytest.fixture
def proxy(upstream, intercepted_localhost):
    upstream_url, _ = upstream
    server = ProxyServer(host="127.0.0.1", port=free_port())
    server.start(blocking=False)
    _wait_for_port(server.port)
    yield server, upstream_url
    server.stop()
    time.sleep(0.2)


def _send(body: dict, upstream_url: str, proxy_port: int, *, claude_code: bool = False) -> None:
    """Send in absolute-URI proxy form so the target is the mock upstream.

    No ``X-Claude-Code-Session-Id`` header by default: ``_classify_route``
    falls through to ``"tokenpak"`` -> the default / SDK policy -> the
    json_inject branch this suite targets. Pass ``claude_code=True`` only for
    the one test that deliberately compares both routes.
    """
    headers = {
        "Content-Type": "application/json",
        "x-api-key": "test-key",
        "anthropic-version": "2023-06-01",
    }
    if claude_code:
        headers["X-Claude-Code-Session-Id"] = "cross-route-comparison"
    req = urllib.request.Request(
        f"{upstream_url}/v1/messages",
        data=json.dumps(body).encode(),
        headers=headers,
        method="POST",
    )
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": f"http://127.0.0.1:{proxy_port}"})
    )
    try:
        opener.open(req, timeout=10).read()
    except Exception:
        # Upstream shape errors are irrelevant — the recording is what matters.
        pass


def _eligible_request() -> dict:
    """A request large enough to clear the retrieval eligibility floor."""
    return {
        "model": "claude-sonnet-4-6",
        "max_tokens": 64,
        "system": [{"type": "text", "text": "You are a helpful assistant."}],
        "messages": [{"role": "user", "content": _FILLER + " what routes to the payments API?"}],
    }


# ---------------------------------------------------------------------------


def test_the_harness_records_what_upstream_receives(proxy, upstream):
    """Guard the instrument itself before trusting anything it reports."""
    server, upstream_url = proxy
    _, recorder = upstream

    _send(_eligible_request(), upstream_url, server.port)

    assert recorder.received_bodies, (
        "the mock upstream recorded nothing — the request never arrived, so any "
        "conclusion about injection drawn from this suite would be meaningless"
    )


def test_vault_content_reaches_the_provider_on_the_json_inject_route(
    proxy, upstream, vault_with_content
):
    """Policy-on: a route whose policy declares json_inject actually gets it.

    This is the live defect the branch closed — route policy had declared
    this for every OpenClaw/SDK request, and nothing ever executed it.
    """
    server, upstream_url = proxy
    _, recorder = upstream

    _send(_eligible_request(), upstream_url, server.port)

    assert recorder.received_bodies, "nothing reached the upstream"
    sent = b"".join(recorder.received_bodies).decode("utf-8", "replace")

    assert VAULT_MARKER in sent, (
        "vault content did not reach the provider on the json_inject route — "
        "retrieval reported content, and the request went upstream without it"
    )
    assert "what routes to the payments API?" in sent, "the user's own prompt was lost"

    # The stage having run (and not skipped) is implied by the marker's
    # presence above; the real session counter corroborates it independently.
    assert server.session["injection_hits"] == 1


def test_the_session_write_happens_exactly_once(proxy, upstream, vault_with_content):
    """Regression guard: exactly one accounting write per injecting request.

    The call site this branch added must not write to session accounting
    itself — the one existing centralized write, made under the session
    lock alongside every other per-request counter, already does that. A
    duplicate call from the new branch is the exact defect the update-by-merge
    inventory found: the pre-existing branch content called the writer with
    the old two-argument shape, which raised on every request (silently, via
    the enclosing fail-open except) and, being otherwise inert, could regress
    again without an assertion this direct. A reintroduced duplicate call
    would double ``injection_hits`` (and, unless it also happened to raise,
    ``injected_tokens``) for this single request — asserted directly against
    the real ``ProxyServer.session`` a live request actually accumulates onto,
    the same object ``GET /stats`` and ``tokenpak status`` read.
    """
    server, upstream_url = proxy

    _send(_eligible_request(), upstream_url, server.port)

    session = server.session
    assert session["injection_hits"] == 1
    # Not asserting an exact token count: the stage recomputes it from the real
    # tokenizer over the (possibly skeleton-extracted) injection text, not the
    # raw value this fixture's stub returned — that recount is real system
    # behavior, not something this test should pin.
    assert session["injected_tokens"] > 0
    assert session["injected_source_names"] == ["decisions/routing.md"]


def test_policy_off_switch_leaves_the_body_untouched_at_the_injection_boundary(
    proxy, upstream, vault_with_content
):
    """Policy-off (master switch off): the injection boundary is a true no-op.

    Vault content is stubbed and available here specifically to prove the
    switch, not the absence of retrievable content, is what gates this —
    identical setup to the policy-on test above except for one flag.

    Asserting the marker is absent from what reaches the upstream, and that
    the real session counters stay at zero, captures the no-op at the
    boundary #634 is responsible for without needing to intercept the
    stage's own call. The full response the client receives also passes
    through the compression hook and cache-control stamping, which apply
    regardless of vault injection and are out of scope here.
    """
    previous = _reload_vault_config_with_switch(False)
    try:
        server, upstream_url = proxy
        _, recorder = upstream

        _send(_eligible_request(), upstream_url, server.port)

        assert recorder.received_bodies, "nothing reached the upstream"
        sent = b"".join(recorder.received_bodies).decode("utf-8", "replace")
        assert VAULT_MARKER not in sent
        assert "what routes to the payments API?" in sent, "the user's own prompt was lost"

        # The one centralized writer runs unconditionally for every request
        # (it is a no-op below its own `injected_tokens <= 0` guard) — what
        # must NOT happen is that call recording a nonzero hit.
        session = server.session
        assert session.get("injected_tokens", 0) == 0
        assert session.get("injection_hits", 0) == 0
    finally:
        _restore_vault_config(previous)


def test_a_retrieval_failure_fails_open_and_the_request_still_completes(
    proxy, upstream, vault_raises
):
    """Fail-open: an exception raised inside retrieval must never break the
    request or corrupt accounting on the json_inject route.

    ``compile_injection`` here raises before ``stage_vault_injection`` ever
    assigns a mutated body, and that raise is not caught until the
    ``except Exception: pass`` around the call site in ``_proxy_to_inner`` —
    this is the seam that guarantee actually depends on for this route.
    """
    server, upstream_url = proxy
    _, recorder = upstream

    _send(_eligible_request(), upstream_url, server.port)

    assert recorder.received_bodies, "the request must still reach the upstream despite the failure"
    sent = b"".join(recorder.received_bodies).decode("utf-8", "replace")
    assert VAULT_MARKER not in sent
    assert "what routes to the payments API?" in sent, "the user's own prompt was lost"

    # The one centralized writer still runs (it is a no-op below its own
    # `injected_tokens <= 0` guard) — a failed injection must not be recorded
    # as a hit. Checked against the real session, the same object a
    # duplicate/wrongly-shaped call would also have to corrupt to go unnoticed.
    session = server.session
    assert session.get("injected_tokens", 0) == 0
    assert session.get("injection_hits", 0) == 0


def test_injection_reaches_the_provider_uniformly_on_both_declared_routes(
    proxy, upstream, vault_with_content
):
    """The property duty (3) actually asks for: uniform application.

    Two requests, same stubbed vault content, differing only in whether
    ``X-Claude-Code-Session-Id`` is present — which is the sole thing that
    routes one onto byte_splice and the other onto json_inject. Both must
    carry the marker upstream; before this branch, only the first did.
    """
    server, upstream_url = proxy
    _, recorder = upstream

    _send(_eligible_request(), upstream_url, server.port, claude_code=True)
    _send(_eligible_request(), upstream_url, server.port, claude_code=False)

    assert len(recorder.received_bodies) == 2, "both requests must reach the upstream"
    byte_splice_sent = recorder.received_bodies[0].decode("utf-8", "replace")
    json_inject_sent = recorder.received_bodies[1].decode("utf-8", "replace")

    assert VAULT_MARKER in byte_splice_sent, "the byte-preserved route regressed"
    assert VAULT_MARKER in json_inject_sent, "the json_inject route never received it"
