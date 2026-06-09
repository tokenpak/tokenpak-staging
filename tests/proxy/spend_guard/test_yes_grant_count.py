# SPDX-License-Identifier: Apache-2.0
"""allow=N count-grant tests (Standard 29 §"Yes-grant scope" — count grants).

``[TIP: allow=<N>]`` or a bare integer reply ("20") pre-approves the next N
blocked sends — N behaves exactly like answering "yes" N times. The held
request being approved is send #1, so the grant covers the remaining N-1
sends, decrementing once per redemption and re-prompting at zero.

These tests pin:
  * grammar: allow=N parses to a positive int; 0/negative/non-integer ignored
  * a count reply (TIP or bare integer) replays + opens a count grant
  * decrement-to-zero, then normal prompting resumes
  * invalid / zero / negative counts open NO grant
  * dollar-budget coexistence — both ceilings bind, first floor wins
  * hard-cap interaction — count grants cross neither hard-block nor rolling caps
  * backward compat — allow=1 == allow=once (single approval, no grant)
"""

from __future__ import annotations

import json

import pytest

from tokenpak.proxy.spend_guard import evaluate
from tokenpak.proxy.spend_guard.audit import query_recent
from tokenpak.proxy.spend_guard.grants import (
    EXHAUSTED,
    NO_GRANT,
    REDEEMED,
    GrantStore,
)
from tokenpak.proxy.spend_guard.policy import SpendGuardConfig
from tokenpak.proxy.spend_guard.tip_header import parse_tip_header

# --------------------------------------------------------------------------
# Fixtures / helpers (mirror test_yes_grant_session_scoped.py)
# --------------------------------------------------------------------------

@pytest.fixture
def tmp_cfg(tmp_path):
    cfg = SpendGuardConfig()
    cfg.audit_db_path = str(tmp_path / "spend_guard.db")
    cfg.session_block_cost_usd = 0.0
    return cfg


def _opus_body(content="hi", max_tokens: int = 4000) -> bytes:
    if isinstance(content, int):
        content = "x" * content
    return json.dumps({
        "model": "claude-opus-4-7",
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": content}],
    }).encode()


def _runaway(fill: str = "x", n: int = 740_000) -> bytes:
    return _opus_body(fill * n, max_tokens=4000)


def _events(cfg, session_id):
    return [r["event_type"]
            for r in query_recent(cfg.audit_db_path, session_id=session_id, limit=50)]


# --------------------------------------------------------------------------
# Grammar: allow=N parses; invalid values are ignored
# --------------------------------------------------------------------------

class TestAllowCountGrammar:
    def test_positive_integer_sets_count_not_scope(self):
        d, _ = parse_tip_header("[TIP: allow=20]")
        assert d.allow_count == 20
        assert d.allow_scope is None

    def test_one_is_a_count_of_one(self):
        d, _ = parse_tip_header("[TIP: allow=1]")
        assert d.allow_count == 1
        assert d.allow_scope is None

    @pytest.mark.parametrize("token", ["allow=0", "allow=-5", "allow=abc", "allow=1.5"])
    def test_invalid_counts_ignored(self, token):
        d, _ = parse_tip_header(f"[TIP: {token}]")
        assert d.allow_count is None
        assert d.allow_scope is None

    def test_on_true_still_session_not_count(self):
        # Ergonomic boolean aliases stay session-scoped (not a count).
        for v in ("on", "true"):
            d, _ = parse_tip_header(f"[TIP: allow={v}]")
            assert d.allow_scope == "session"
            assert d.allow_count is None

    def test_named_scopes_unchanged(self):
        for v in ("once", "15m", "session"):
            d, _ = parse_tip_header(f"[TIP: allow={v}]")
            assert d.allow_scope == v
            assert d.allow_count is None


# --------------------------------------------------------------------------
# A count reply (TIP or bare integer) replays + opens a count grant
# --------------------------------------------------------------------------

class TestCountReplyOpensGrant:
    def test_tip_allow_n_replays_and_grants(self, tmp_cfg):
        sid = "sess-count-tip"
        out1 = evaluate(_runaway("a"), "claude-opus-4-7", sid, {}, config=tmp_cfg)
        assert out1.kind == "block"
        out2 = evaluate(_opus_body("[TIP: allow=5]"), "claude-opus-4-7", sid, {}, config=tmp_cfg)
        assert out2.kind == "replay"
        assert "yes_grant_created" in _events(tmp_cfg, sid)
        # A distinct runaway is forwarded by the active count grant.
        out3 = evaluate(_runaway("b"), "claude-opus-4-7", sid, {}, config=tmp_cfg)
        assert out3.kind in ("forward", "forward_modified")
        assert out3.audit_event == "yes_grant_bypass"

    def test_bare_integer_reply_replays_and_grants(self, tmp_cfg):
        sid = "sess-count-bare"
        evaluate(_runaway("a"), "claude-opus-4-7", sid, {}, config=tmp_cfg)
        # A bare "20" is an approval that also pre-approves the next sends.
        out2 = evaluate(_opus_body("20"), "claude-opus-4-7", sid, {}, config=tmp_cfg)
        assert out2.kind == "replay"
        assert "yes_grant_created" in _events(tmp_cfg, sid)
        out3 = evaluate(_runaway("b"), "claude-opus-4-7", sid, {}, config=tmp_cfg)
        assert out3.audit_event == "yes_grant_bypass"


# --------------------------------------------------------------------------
# N behaves like answering yes N times — decrement to zero, then re-prompt
# --------------------------------------------------------------------------

class TestDecrementToZero:
    def test_allow_3_covers_exactly_three_sends(self, tmp_cfg):
        sid = "sess-count-3"
        # Block, then approve with a count of 3.
        assert evaluate(_runaway("a"), "claude-opus-4-7", sid, {}, config=tmp_cfg).kind == "block"
        assert evaluate(_opus_body("[TIP: allow=3]"), "claude-opus-4-7", sid, {}, config=tmp_cfg).kind == "replay"  # send #1
        # Sends #2 and #3 ride the grant.
        assert evaluate(_runaway("b"), "claude-opus-4-7", sid, {}, config=tmp_cfg).kind in ("forward", "forward_modified")  # #2
        assert evaluate(_runaway("c"), "claude-opus-4-7", sid, {}, config=tmp_cfg).kind in ("forward", "forward_modified")  # #3
        # The grant is spent — send #4 re-prompts.
        assert evaluate(_runaway("d"), "claude-opus-4-7", sid, {}, config=tmp_cfg).kind == "block"

    def test_store_decrements_then_no_grant(self, tmp_path):
        store = GrantStore(str(tmp_path / "g.db"))
        store.create(session_id="s", fleet_id="", agent_id="a",
                     ttl_seconds=300, remaining_count=2, now=1000.0)
        status, g = store.redeem("s", "", "a", 0.10, now=1001.0)
        assert status == REDEEMED
        assert g.remaining_count == 1
        status, g = store.redeem("s", "", "a", 0.10, now=1002.0)
        assert status == REDEEMED
        assert g.remaining_count == 0
        # Drained → row deleted, next request re-prompts.
        status, _ = store.redeem("s", "", "a", 0.10, now=1003.0)
        assert status == NO_GRANT


# --------------------------------------------------------------------------
# Invalid / zero / negative counts open NO grant (normal prompting)
# --------------------------------------------------------------------------

class TestInvalidCountNoGrant:
    @pytest.mark.parametrize("reply", ["[TIP: allow=0]", "[TIP: allow=-4]", "0"])
    def test_invalid_count_does_not_grant(self, tmp_cfg, reply):
        sid = f"sess-bad-{abs(hash(reply))}"
        evaluate(_runaway("a"), "claude-opus-4-7", sid, {}, config=tmp_cfg)
        out = evaluate(_opus_body(reply), "claude-opus-4-7", sid, {}, config=tmp_cfg)
        # No count → not an approval → held request stays pending (reprompt).
        assert out.kind == "reprompt"
        assert "yes_grant_created" not in _events(tmp_cfg, sid)


# --------------------------------------------------------------------------
# Dollar-budget coexistence — both ceilings bind, first floor wins
# --------------------------------------------------------------------------

class TestDollarCountCoexistence:
    def test_dollar_floor_hit_before_count(self, tmp_path):
        store = GrantStore(str(tmp_path / "g.db"))
        store.create(session_id="s", fleet_id="", agent_id="a", ttl_seconds=300,
                     remaining_count=5, max_cost_usd=1.0, now=1000.0)
        s, g = store.redeem("s", "", "a", 0.40, now=1001.0)
        assert s == REDEEMED and g.remaining_count == 4 and g.max_cost_usd_remaining == pytest.approx(0.60)
        s, g = store.redeem("s", "", "a", 0.40, now=1002.0)
        assert s == REDEEMED and g.remaining_count == 3 and g.max_cost_usd_remaining == pytest.approx(0.20)
        # $0.30 can't be covered by $0.20 → dollar floor exhausts the grant even
        # though 3 count remained.
        s, _ = store.redeem("s", "", "a", 0.30, now=1003.0)
        assert s == EXHAUSTED
        assert store.redeem("s", "", "a", 0.01, now=1004.0)[0] == NO_GRANT

    def test_count_floor_hit_before_dollars(self, tmp_path):
        store = GrantStore(str(tmp_path / "g.db"))
        store.create(session_id="s", fleet_id="", agent_id="a", ttl_seconds=300,
                     remaining_count=1, max_cost_usd=100.0, now=1000.0)
        s, g = store.redeem("s", "", "a", 0.10, now=1001.0)
        assert s == REDEEMED and g.remaining_count == 0
        # Count drained though ~$99.90 of budget remained.
        assert store.redeem("s", "", "a", 0.10, now=1002.0)[0] == NO_GRANT


# --------------------------------------------------------------------------
# Hard-cap interaction — count grants cross neither hard-block nor rolling caps
# --------------------------------------------------------------------------

class TestCountHardCapInteraction:
    def test_count_grant_does_not_cover_hard_block(self, tmp_cfg):
        sid = "sess-count-hardblock"
        evaluate(_runaway("a"), "claude-opus-4-7", sid, {}, config=tmp_cfg)
        evaluate(_opus_body("[TIP: allow=5]"), "claude-opus-4-7", sid, {}, config=tmp_cfg)
        hard = _opus_body("x" * 4_000_000, max_tokens=50_000)
        out = evaluate(hard, "claude-opus-4-7", sid, {}, config=tmp_cfg)
        assert out.kind == "hard_block"

    def test_count_grant_does_not_bypass_rolling_cap(self, tmp_cfg):
        sid = "sess-count-rollingcap"
        # Default posture: a Yes/count grant does NOT cover rolling caps.
        assert tmp_cfg.yes_grant_covers_rolling_caps is False
        tmp_cfg.rolling_caps_enabled = True
        tmp_cfg.rolling_caps_per_agent_max_cost_usd = 1e-6  # any send breaches
        # Pre-arm a count grant for this exact composite key.
        GrantStore(tmp_cfg.audit_db_path).create(
            session_id=sid, fleet_id="", agent_id="a1",
            ttl_seconds=300, remaining_count=5,
        )
        out = evaluate(_runaway("a"), "claude-opus-4-7", sid,
                       {"x-tokenpak-agent": "a1"}, config=tmp_cfg)
        # The rolling cap blocks despite the active count grant.
        assert out.kind == "block"
        assert out.audit_event == "rolling_cap_block"


# --------------------------------------------------------------------------
# Backward compat — allow=1 == allow=once (single approval, no grant)
# --------------------------------------------------------------------------

class TestAllowOneIsSingleApproval:
    def test_allow_one_opens_no_grant(self, tmp_cfg):
        sid = "sess-count-1"
        evaluate(_runaway("a"), "claude-opus-4-7", sid, {}, config=tmp_cfg)
        out2 = evaluate(_opus_body("[TIP: allow=1]"), "claude-opus-4-7", sid, {}, config=tmp_cfg)
        assert out2.kind == "replay"
        # N==1 is a single approval — no multi-request grant is opened.
        assert "yes_grant_created" not in _events(tmp_cfg, sid)
        # The next runaway blocks again (single-request semantics held).
        assert evaluate(_runaway("b"), "claude-opus-4-7", sid, {}, config=tmp_cfg).kind == "block"


# --------------------------------------------------------------------------
# Proactive prepend: [TIP: allow=N] on a FRESH request pre-arms the full count
# --------------------------------------------------------------------------

class TestProactivePrependArmsFullCount:
    def test_fresh_prepend_arms_grant_and_forwards(self, tmp_cfg):
        sid = "sess-proactive-arm"
        # No prior 402 — a fresh request that carries [TIP: allow=3].
        out = evaluate(_opus_body("[TIP: allow=3] do the thing"),
                       "claude-opus-4-7", sid, {}, config=tmp_cfg)
        # This send (#1) goes through, and a count grant is armed.
        assert out.kind in ("forward", "forward_modified")
        assert "yes_grant_created" in _events(tmp_cfg, sid)
        # Grant carries remaining_count = N-1 = 2 for the (sid, "", "") key.
        grant = GrantStore(tmp_cfg.audit_db_path).get_active(sid, "", "")
        assert grant is not None and grant.remaining_count == 2

    def test_fresh_prepend_covers_next_n_minus_one_sends(self, tmp_cfg):
        sid = "sess-proactive-ride"
        # Prepend allow=3 on a fresh send (#1) → next two blocked sends ride.
        assert evaluate(_opus_body("[TIP: allow=3] go"), "claude-opus-4-7", sid, {}, config=tmp_cfg).kind in ("forward", "forward_modified")  # #1
        assert evaluate(_runaway("a"), "claude-opus-4-7", sid, {}, config=tmp_cfg).kind in ("forward", "forward_modified")  # #2
        assert evaluate(_runaway("b"), "claude-opus-4-7", sid, {}, config=tmp_cfg).kind in ("forward", "forward_modified")  # #3
        # Grant spent — the 4th send re-prompts.
        assert evaluate(_runaway("c"), "claude-opus-4-7", sid, {}, config=tmp_cfg).kind == "block"

    def test_fresh_prepend_allow_one_opens_no_grant(self, tmp_cfg):
        sid = "sess-proactive-1"
        out = evaluate(_opus_body("[TIP: allow=1] go"), "claude-opus-4-7", sid, {}, config=tmp_cfg)
        assert out.kind in ("forward", "forward_modified")
        # allow=1 is a single send — no grant armed.
        assert "yes_grant_created" not in _events(tmp_cfg, sid)
        assert GrantStore(tmp_cfg.audit_db_path).get_active(sid, "", "") is None

    @pytest.mark.parametrize("token", ["allow=0", "allow=-2", "allow=nan"])
    def test_fresh_prepend_invalid_count_opens_no_grant(self, tmp_cfg, token):
        sid = f"sess-proactive-bad-{abs(hash(token))}"
        evaluate(_opus_body(f"[TIP: {token}] go"), "claude-opus-4-7", sid, {}, config=tmp_cfg)
        assert "yes_grant_created" not in _events(tmp_cfg, sid)
        assert GrantStore(tmp_cfg.audit_db_path).get_active(sid, "", "") is None

    def test_fresh_prepend_does_not_cross_hard_block(self, tmp_cfg):
        sid = "sess-proactive-hardblock"
        # allow=5 must NOT push a hard-block-band request through, nor arm a grant.
        hard = _opus_body("[TIP: allow=5] " + "x" * 4_000_000, max_tokens=50_000)
        out = evaluate(hard, "claude-opus-4-7", sid, {}, config=tmp_cfg)
        assert out.kind == "hard_block"
        assert GrantStore(tmp_cfg.audit_db_path).get_active(sid, "", "") is None

    def test_fresh_prepend_does_not_bypass_rolling_cap(self, tmp_cfg):
        sid = "sess-proactive-rollingcap"
        tmp_cfg.rolling_caps_enabled = True
        tmp_cfg.rolling_caps_per_agent_max_cost_usd = 1e-6  # any send breaches
        out = evaluate(_opus_body("[TIP: allow=5] go"), "claude-opus-4-7", sid,
                       {"x-tokenpak-agent": "a1"}, config=tmp_cfg)
        assert out.kind == "block"
        assert out.audit_event == "rolling_cap_block"
        # No grant armed when the send itself was rolling-cap blocked.
        assert GrantStore(tmp_cfg.audit_db_path).get_active(sid, "", "a1") is None
