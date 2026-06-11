# SPDX-License-Identifier: Apache-2.0
"""Tests for the ``[TIP: deterministic=on]`` directive (reproducible eval mode).

Coverage map (per the TIP versioning standard, reproducible-eval contract):

  - header parse / validation (incl. fail-loud on unsupported values)
  - DIRECTIVE_REGISTRY presence
  - precedence interactions with allow / estimate (policy contract)
  - behavioral effect on the cache path (SemanticCache substitution disabled)
  - behavioral effect on the retry path (RetryEngine single-attempt fail-loud)
  - server preflight / fingerprint / metadata-header surface
  - no-directive default behavior unchanged (regression sentinels)
"""

from __future__ import annotations

import json
import re

import pytest

from tokenpak.cache.semantic_cache import SemanticCache, SemanticCacheConfig
from tokenpak.orchestration import retry as retry_module
from tokenpak.orchestration.retry import RetryEngine, RetryExhaustedError
from tokenpak.proxy.server import (
    _deterministic_finalize,
    _deterministic_preflight,
    _deterministic_response_headers,
)
from tokenpak.proxy.spend_guard.contracts import RiskEstimate, TIPDirective
from tokenpak.proxy.spend_guard.policy import (
    SpendGuardConfig,
    decide,
    deterministic_precedence,
)
from tokenpak.proxy.spend_guard.tip_header import (
    DIRECTIVE_REGISTRY,
    FINGERPRINT_STRIP_FIELDS,
    canonicalize_request_for_fingerprint,
    compute_request_fingerprint,
    parse_and_strip_tip_header,
    parse_tip_header,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _anthropic_body(first_user_text: str, **extra) -> bytes:
    payload = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 64,
        "messages": [{"role": "user", "content": first_user_text}],
    }
    payload.update(extra)
    return json.dumps(payload).encode("utf-8")


def _small_estimate() -> RiskEstimate:
    return RiskEstimate(
        model="claude-sonnet-4-6",
        current_context_tokens=100,
        request_tokens=100,
        projected_input_tokens=200,
        projected_output_tokens=50,
        projected_cost_usd=0.01,
        cache_hit_ratio=0.0,
        rates={},
    )


def _large_estimate() -> RiskEstimate:
    # ≥ 90% of a 1M context window → context-window-% soft block.
    return RiskEstimate(
        model="claude-sonnet-4-6",
        current_context_tokens=900_000,
        request_tokens=50_000,
        projected_input_tokens=950_000,
        projected_output_tokens=1_000,
        projected_cost_usd=3.0,
        cache_hit_ratio=0.0,
        rates={},
    )


# ---------------------------------------------------------------------------
# Registry + header parse / validation
# ---------------------------------------------------------------------------


class TestRegistryAndParse:
    def test_registry_contains_deterministic(self):
        assert "deterministic" in DIRECTIVE_REGISTRY
        handler, doc = DIRECTIVE_REGISTRY["deterministic"]
        assert callable(handler)
        assert "eval" in doc.lower() or "reproducible" in doc.lower()

    @pytest.mark.parametrize("value", ["on", "true", "1", "yes"])
    def test_parse_on_values(self, value):
        d, rem = parse_tip_header(f"[TIP: deterministic={value}] hello")
        assert d is not None
        assert d.deterministic is True
        assert d.deterministic_invalid_value is None
        assert rem == "hello"

    def test_parse_bare_key_enables(self):
        d, _ = parse_tip_header("[TIP: deterministic] hello")
        assert d is not None
        assert d.deterministic is True

    @pytest.mark.parametrize("value", ["off", "false", "0", "no"])
    def test_parse_off_values(self, value):
        d, _ = parse_tip_header(f"[TIP: deterministic={value}] hello")
        assert d is not None
        assert d.deterministic is False
        assert d.deterministic_invalid_value is None

    @pytest.mark.parametrize("value", ["maybe", "2", "always", "ON_PLEASE"])
    def test_unsupported_value_marks_fail_loud_never_silent(self, value):
        """Unsupported deterministic values must be recorded for rejection,
        not silently dropped (reproducible-eval fail-loud contract)."""
        d, _ = parse_tip_header(f"[TIP: deterministic={value}] hello")
        assert d is not None
        assert d.deterministic is False
        assert d.deterministic_invalid_value == value
        # Not routed through the unknown-keys (silent-ish) path either.
        assert all("deterministic" not in k for k in d.unknown_keys)

    def test_parse_combined_with_estimate(self):
        d, _ = parse_tip_header("[TIP: deterministic=on estimate=on] run it")
        assert d is not None
        assert d.deterministic is True
        assert d.estimate_only is True

    def test_parse_combined_with_allow(self):
        d, _ = parse_tip_header("[TIP: deterministic=on allow=once] go")
        assert d is not None
        assert d.deterministic is True
        assert d.allow_scope == "once"

    def test_strip_from_first_user_message_of_json_body(self):
        body = _anthropic_body("[TIP: deterministic=on] what is 2+2?")
        d, stripped = parse_and_strip_tip_header(body)
        assert d is not None and d.deterministic is True
        parsed = json.loads(stripped)
        assert parsed["messages"][0]["content"] == "what is 2+2?"
        assert b"[TIP:" not in stripped

    def test_mid_sentence_token_is_content_not_directive(self):
        body = _anthropic_body("please explain [TIP: deterministic=on] syntax")
        d, modified = parse_and_strip_tip_header(body)
        assert d is None
        assert modified is body  # zero-cost identity path

    def test_no_directive_default_unchanged(self):
        """Regression sentinel: bodies without a TIP header are untouched."""
        body = _anthropic_body("just a normal request")
        d, modified = parse_and_strip_tip_header(body)
        assert d is None
        assert modified is body

    def test_directive_dataclass_defaults(self):
        d = TIPDirective()
        assert d.deterministic is False
        assert d.deterministic_invalid_value is None


# ---------------------------------------------------------------------------
# Precedence (policy contract)
# ---------------------------------------------------------------------------


class TestPrecedence:
    def test_none_tip_compatible(self):
        assert deterministic_precedence(None) is None

    def test_deterministic_alone_compatible(self):
        assert deterministic_precedence(TIPDirective(deterministic=True)) is None

    def test_estimate_compatible(self):
        tip = TIPDirective(deterministic=True, estimate_only=True)
        assert deterministic_precedence(tip) is None

    def test_cancel_compatible(self):
        tip = TIPDirective(deterministic=True, cancel=True)
        assert deterministic_precedence(tip) is None

    @pytest.mark.parametrize("scope", ["once", "15m", "session"])
    def test_allow_scope_incompatible(self, scope):
        tip = TIPDirective(deterministic=True, allow_scope=scope)
        conflict = deterministic_precedence(tip)
        assert conflict == f"deterministic_conflict:allow={scope}"

    def test_allow_count_incompatible(self):
        tip = TIPDirective(deterministic=True, allow_count=5)
        assert deterministic_precedence(tip) == "deterministic_conflict:allow=5"

    def test_bypass_incompatible(self):
        tip = TIPDirective(deterministic=True, bypass=True)
        assert deterministic_precedence(tip) == "deterministic_conflict:bypass"

    def test_max_ttl_alone_compatible_inert(self):
        tip = TIPDirective(deterministic=True, max_cost_usd=5.0, ttl_seconds=60)
        assert deterministic_precedence(tip) is None

    def test_allow_without_deterministic_is_not_a_conflict(self):
        """The conflict only exists when deterministic mode is requested."""
        tip = TIPDirective(allow_scope="once")
        assert deterministic_precedence(tip) is None


class TestPolicyDecideUnchanged:
    """deterministic=on is NOT a spend directive — decide() must behave
    exactly as with tip=None, in both the allow and block bands."""

    def test_small_request_allows_with_and_without_deterministic(self):
        cfg = SpendGuardConfig()
        baseline = decide(_small_estimate(), cfg, tip=None)
        with_det = decide(
            _small_estimate(), cfg, tip=TIPDirective(deterministic=True)
        )
        assert baseline.decision == "allow"
        assert with_det.decision == baseline.decision
        assert with_det.reason == baseline.reason

    def test_large_request_blocks_with_and_without_deterministic(self):
        cfg = SpendGuardConfig()
        baseline = decide(
            _large_estimate(), cfg, tip=None, model_max_context_tokens=1_000_000
        )
        with_det = decide(
            _large_estimate(),
            cfg,
            tip=TIPDirective(deterministic=True),
            model_max_context_tokens=1_000_000,
        )
        assert baseline.decision == "block"
        assert with_det.decision == "block"
        assert with_det.reason == baseline.reason
        assert with_det.requires_approval is True

    def test_allow_once_still_bypasses_without_deterministic(self):
        """Contrast case: a plain allow=once TIP takes the TIP-ceiling path —
        proving deterministic-only directives do NOT take it."""
        cfg = SpendGuardConfig()
        decision = decide(
            _large_estimate(),
            cfg,
            tip=TIPDirective(allow_scope="once"),
            model_max_context_tokens=1_000_000,
        )
        assert decision.decision == "allow"
        assert decision.threshold_hit == "tip_directive"


# ---------------------------------------------------------------------------
# Request fingerprint
# ---------------------------------------------------------------------------


class TestFingerprint:
    def test_format(self):
        fp = compute_request_fingerprint(_anthropic_body("hi"))
        assert re.fullmatch(r"sha256:[0-9a-f]{64}", fp)

    def test_documented_strip_list_is_exact(self):
        assert FINGERPRINT_STRIP_FIELDS == frozenset(
            {"metadata", "stream", "nonce", "timestamp", "request_id", "idempotency_key"}
        )

    @pytest.mark.parametrize(
        "field,value_a,value_b",
        [
            ("metadata", {"user_id": "alice"}, {"user_id": "bob"}),
            ("stream", True, False),
            ("nonce", "abc123", "def456"),
            ("timestamp", 1718000000, 1718099999),
            ("request_id", "req-1", "req-2"),
            ("idempotency_key", "k1", "k2"),
        ],
    )
    def test_stable_across_volatile_fields(self, field, value_a, value_b):
        a = _anthropic_body("same question", **{field: value_a})
        b = _anthropic_body("same question", **{field: value_b})
        assert compute_request_fingerprint(a) == compute_request_fingerprint(b)

    def test_sensitive_to_message_content(self):
        a = _anthropic_body("question one")
        b = _anthropic_body("question two")
        assert compute_request_fingerprint(a) != compute_request_fingerprint(b)

    def test_sensitive_to_generation_params(self):
        a = _anthropic_body("q", temperature=0.0)
        b = _anthropic_body("q", temperature=1.0)
        assert compute_request_fingerprint(a) != compute_request_fingerprint(b)

    def test_key_order_insensitive(self):
        a = b'{"model":"m","messages":[]}'
        b = b'{"messages":[],"model":"m"}'
        assert compute_request_fingerprint(a) == compute_request_fingerprint(b)

    def test_non_json_body_does_not_crash(self):
        fp = compute_request_fingerprint(b"\x00\x01 not json")
        assert fp.startswith("sha256:")

    def test_canonicalization_strips_only_top_level(self):
        body = json.dumps(
            {"model": "m", "messages": [{"role": "user", "content": "stream"}], "stream": True}
        ).encode()
        canon = json.loads(canonicalize_request_for_fingerprint(body))
        assert "stream" not in canon
        assert canon["messages"][0]["content"] == "stream"  # nested untouched


# ---------------------------------------------------------------------------
# Cache path — semantic response substitution disabled
# ---------------------------------------------------------------------------


class TestSemanticCacheDeterministic:
    def _seeded_cache(self) -> SemanticCache:
        cache = SemanticCache(SemanticCacheConfig(enabled=True, ttl_seconds=300))
        cache.store("What is the capital of France?", b'{"answer":"Paris"}',
                    "application/json", "json")
        return cache

    def test_default_lookup_still_hits(self):
        """Regression sentinel: normal-mode behavior unchanged."""
        cache = self._seeded_cache()
        result = cache.lookup("What is the capital of France?", expected_format="json")
        assert result.hit is True
        assert result.match_strategy == "exact"

    def test_deterministic_lookup_is_forced_miss(self):
        cache = self._seeded_cache()
        result = cache.lookup(
            "What is the capital of France?",
            expected_format="json",
            deterministic=True,
        )
        assert result.hit is False
        assert result.entry is None
        assert result.match_strategy == "deterministic_bypass"

    def test_deterministic_lookup_does_not_pollute_stats(self):
        cache = self._seeded_cache()
        before = cache.stats()
        cache.lookup("What is the capital of France?", expected_format="json",
                     deterministic=True)
        after = cache.stats()
        assert after["hits"] == before["hits"]
        assert after["misses"] == before["misses"]

    def test_deterministic_store_is_noop(self):
        cache = SemanticCache(SemanticCacheConfig(enabled=True))
        entry = cache.store("eval query", b'{"x":1}', "application/json", "json",
                            deterministic=True)
        assert entry is None
        assert cache.size() == 0
        # And the entry is not servable to normal traffic afterwards.
        assert cache.lookup("eval query", expected_format="json").hit is False

    def test_default_store_unchanged(self):
        cache = SemanticCache(SemanticCacheConfig(enabled=True))
        entry = cache.store("q", b'{"x":1}', "application/json", "json")
        assert entry is not None
        assert cache.size() == 1


# ---------------------------------------------------------------------------
# Retry path — single attempt, fail loud
# ---------------------------------------------------------------------------


class TestRetryEngineDeterministic:
    @pytest.fixture(autouse=True)
    def _isolate_event_log(self, tmp_path, monkeypatch):
        monkeypatch.setattr(retry_module, "RETRY_EVENT_LOG", tmp_path / "events.jsonl")

    def test_success_first_attempt_returns_result(self, tmp_path):
        calls = []

        def fn(context, partial_state):
            calls.append(1)
            return "ok"

        engine = RetryEngine(
            fn=fn, context={"task_id": "det-1"}, state_dir=tmp_path,
            deterministic=True,
        )
        assert engine.run() == "ok"
        assert len(calls) == 1

    def test_failure_is_single_attempt_fail_loud(self, tmp_path):
        calls = []
        alerts = []
        downgrades = []
        switches = []

        def fn(context, partial_state):
            calls.append(1)
            raise RuntimeError("upstream exploded (HTTP 503)")

        engine = RetryEngine(
            fn=fn,
            context={"task_id": "det-2", "task": "eval"},
            state_dir=tmp_path,
            wait_seconds=[0.0, 0.0],
            deterministic=True,
            on_model_downgrade=lambda m: (downgrades.append(m), m)[1],
            on_provider_switch=lambda p: (switches.append(p), p)[1],
            on_human_alert=alerts.append,
        )
        with pytest.raises(RetryExhaustedError):
            engine.run()
        # Exactly ONE attempt: no level-0 retries, no downgrade, no provider
        # switch (route locked; no cross-provider fallback), no handoff.
        assert len(calls) == 1
        assert downgrades == []
        assert switches == []
        # Fail LOUD: human alert fired and the attempt was recorded.
        assert len(alerts) == 1
        assert len(engine.attempts) == 1
        assert "deterministic" in engine.attempts[0].description

    def test_429_does_not_wait_retry_in_deterministic_mode(self, tmp_path):
        calls = []

        def fn(context, partial_state):
            calls.append(1)
            raise RuntimeError("rate limited (429)")

        engine = RetryEngine(
            fn=fn, context={"task_id": "det-3"}, state_dir=tmp_path,
            wait_seconds=[0.0, 0.0, 0.0], deterministic=True,
            on_human_alert=lambda alert: None,
        )
        with pytest.raises(RetryExhaustedError):
            engine.run()
        assert len(calls) == 1

    def test_default_mode_unchanged_retries_then_succeeds(self, tmp_path):
        """Regression sentinel: without the flag, level-0 retry still runs."""
        calls = []

        def fn(context, partial_state):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("transient (HTTP 500)")
            return "recovered"

        engine = RetryEngine(
            fn=fn, context={"task_id": "det-4"}, state_dir=tmp_path,
            wait_seconds=[0.0, 0.0],
        )
        assert engine.run() == "recovered"
        assert len(calls) == 2

    def test_deterministic_default_is_off(self, tmp_path):
        engine = RetryEngine(
            fn=lambda c, p: "x", context={"task_id": "det-5"}, state_dir=tmp_path,
        )
        assert engine.deterministic is False


# ---------------------------------------------------------------------------
# Server surface — preflight, finalize, metadata headers
# ---------------------------------------------------------------------------


class TestServerPreflight:
    def test_empty_and_none_bodies(self):
        assert _deterministic_preflight(None) == (None, None)
        assert _deterministic_preflight(b"") == (None, None)

    def test_body_without_directive(self):
        state, error = _deterministic_preflight(_anthropic_body("plain request"))
        assert state is None and error is None

    def test_deterministic_off_is_normal_mode(self):
        body = _anthropic_body("[TIP: deterministic=off] hello")
        state, error = _deterministic_preflight(body)
        assert state is None and error is None

    def test_other_directives_without_deterministic_pass_through(self):
        body = _anthropic_body("[TIP: allow=once] hello")
        state, error = _deterministic_preflight(body)
        assert state is None and error is None

    def test_active_state_extraction(self):
        body = _anthropic_body(
            "[TIP: deterministic=on] eval prompt",
            temperature=0.0, top_p=1.0, stop_sequences=["END"], seed=42,
        )
        state, error = _deterministic_preflight(body)
        assert error is None
        assert state is not None and state["active"] is True
        assert state["model"] == "claude-sonnet-4-6"
        assert state["seed"] == 42
        assert state["params"]["temperature"] == 0.0
        assert state["params"]["top_p"] == 1.0
        assert state["params"]["stop_sequences"] == ["END"]
        assert state["params"]["max_tokens"] == 64
        assert state["transforms"] == ["tip_header_strip"]
        assert b"[TIP:" not in state["stripped_body"]

    def test_invalid_value_rejected_400(self):
        body = _anthropic_body("[TIP: deterministic=maybe] hello")
        state, error = _deterministic_preflight(body)
        assert state is None
        status, err_type, msg = error
        assert status == 400
        assert err_type == "tokenpak_deterministic_invalid_value"
        assert "maybe" in msg

    @pytest.mark.parametrize(
        "header",
        [
            "[TIP: deterministic=on allow=once]",
            "[TIP: deterministic=on allow=session]",
            "[TIP: deterministic=on allow=5]",
            "[TIP: deterministic=on bypass=on]",
        ],
    )
    def test_conflicting_combination_rejected_400(self, header):
        body = _anthropic_body(f"{header} hello")
        state, error = _deterministic_preflight(body)
        assert state is None
        status, err_type, _ = error
        assert status == 400
        assert err_type == "tokenpak_deterministic_directive_conflict"

    def test_estimate_combination_is_accepted(self):
        body = _anthropic_body("[TIP: deterministic=on estimate=on] hello")
        state, error = _deterministic_preflight(body)
        assert error is None
        assert state is not None and state["active"] is True


class TestServerFinalizeAndHeaders:
    def _active_state(self) -> dict:
        body = _anthropic_body("[TIP: deterministic=on] eval prompt", temperature=0.0)
        state, error = _deterministic_preflight(body)
        assert error is None and state is not None
        return state

    def test_finalize_fingerprint_over_final_body(self):
        state = self._active_state()
        final = state["stripped_body"]
        _deterministic_finalize(state, final, "anthropic")
        assert state["provider"] == "anthropic"
        assert state["fingerprint"] == compute_request_fingerprint(final)
        assert state["prompt_mutation_delta_tokens"] == 0

    def test_finalize_records_mutation_delta_when_body_changed(self):
        state = self._active_state()
        mutated = _anthropic_body("eval prompt " + "PADDING " * 64, temperature=0.0)
        _deterministic_finalize(state, mutated, "anthropic")
        assert state["fingerprint"] == compute_request_fingerprint(mutated)
        assert state["prompt_mutation_delta_tokens"] > 0

    def test_metadata_header_surface(self):
        state = self._active_state()
        _deterministic_finalize(state, state["stripped_body"], "anthropic")
        headers = dict(_deterministic_response_headers(state))
        assert headers["X-TokenPak-Deterministic"] == "on"
        assert headers["X-TokenPak-Deterministic-Fingerprint"].startswith("sha256:")
        assert headers["X-TokenPak-Deterministic-Provider"] == "anthropic"
        assert headers["X-TokenPak-Deterministic-Model"] == "claude-sonnet-4-6"
        assert headers["X-TokenPak-Deterministic-Fallback-Used"] == "false"
        assert headers["X-TokenPak-Deterministic-Retry-Used"] == "false"
        assert headers["X-TokenPak-Deterministic-Cache-Substitution-Used"] == "false"
        assert headers["X-TokenPak-Deterministic-Prompt-Mutation-Delta-Tokens"] == "0"
        assert headers["X-TokenPak-Deterministic-Adapter-Required-Transform"] == "tip_header_strip"
        params = json.loads(headers["X-TokenPak-Deterministic-Params"])
        assert params["temperature"] == 0.0

    def test_seed_header_only_when_client_supplied(self):
        state = self._active_state()  # no seed in body
        _deterministic_finalize(state, state["stripped_body"], "anthropic")
        headers = dict(_deterministic_response_headers(state))
        assert "X-TokenPak-Deterministic-Seed" not in headers

        body = _anthropic_body("[TIP: deterministic=on] q", seed=7)
        seeded, error = _deterministic_preflight(body)
        assert error is None
        _deterministic_finalize(seeded, seeded["stripped_body"], "anthropic")
        seeded_headers = dict(_deterministic_response_headers(seeded))
        assert seeded_headers["X-TokenPak-Deterministic-Seed"] == "7"

    def test_recorded_transform_list_rendered(self):
        state = self._active_state()
        state["transforms"].append("dlp_redact")
        _deterministic_finalize(state, state["stripped_body"], "anthropic")
        headers = dict(_deterministic_response_headers(state))
        assert (
            headers["X-TokenPak-Deterministic-Adapter-Required-Transform"]
            == "tip_header_strip,dlp_redact"
        )
