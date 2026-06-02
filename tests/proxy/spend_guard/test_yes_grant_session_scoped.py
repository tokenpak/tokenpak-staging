# SPDX-License-Identifier: Apache-2.0
"""Session-scoped Yes-grant tests (Standard 29 §"Yes-grant scope", W1-W7).

A single POSITIVE intent (or ``[TIP: allow=session ttl=<sec> max=$<usd>]``)
opens a turn-scoped grant so the rest of an agentic turn skips the soft-block
prompt. These tests pin the hard requirements:

  W1  composite key (session_id, fleet_id, agent_id)
  W2  audit on both ends (created at grant, bypass per redemption)
  W3  grant-table read error fails CLOSED to the block band
  W4  dollar-budget clamp — decrements per redemption, exhausts cleanly
  W5  hard-block band stays non-bypassable by a grant
  W6  cross-session/agent non-reuse
  W7  strict TTL expiry (dead the instant now >= expires_at)

plus acceptance: one yes covers the turn; a NEGATIVE intent tears the grant
down; ``[TIP: allow=once]`` keeps single-request semantics.
"""

from __future__ import annotations

import json

import pytest

from tokenpak.proxy.spend_guard import evaluate
from tokenpak.proxy.spend_guard import grants as grants_mod
from tokenpak.proxy.spend_guard.audit import query_recent
from tokenpak.proxy.spend_guard.grants import (
    EXHAUSTED,
    EXPIRED,
    NO_GRANT,
    REDEEMED,
    GrantStore,
)
from tokenpak.proxy.spend_guard.policy import SpendGuardConfig

# --------------------------------------------------------------------------
# Fixtures / helpers
# --------------------------------------------------------------------------

@pytest.fixture
def tmp_cfg(tmp_path):
    """Isolated audit/pending/grant DB so tests never touch live state."""
    cfg = SpendGuardConfig()
    cfg.audit_db_path = str(tmp_path / "spend_guard.db")
    cfg.session_block_cost_usd = 0.0  # isolate per-request behaviour
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
    """A soft-block-band request (~185K projected input tokens)."""
    return _opus_body(fill * n, max_tokens=4000)


def _events(cfg, session_id):
    return [r["event_type"]
            for r in query_recent(cfg.audit_db_path, session_id=session_id, limit=50)]


# --------------------------------------------------------------------------
# Acceptance (a) + W2: one yes covers the rest of the turn, audited both ends
# --------------------------------------------------------------------------

class TestOneYesCoversTurn:
    def test_yes_grant_lets_subsequent_block_through(self, tmp_cfg):
        sid = "sess-grant-a"
        # 1) First runaway → soft block, pending stored.
        out1 = evaluate(_runaway("a"), "claude-opus-4-7", sid, {}, config=tmp_cfg)
        assert out1.kind == "block"
        # 2) Plain "yes" → replay + opens a turn-scoped grant.
        out2 = evaluate(_opus_body("yes"), "claude-opus-4-7", sid, {}, config=tmp_cfg)
        assert out2.kind == "replay"
        # 3) A *different* runaway (distinct hash, so no anti-loop) would
        #    soft-block on its own — but the active grant forwards it.
        out3 = evaluate(_runaway("b"), "claude-opus-4-7", sid, {}, config=tmp_cfg)
        assert out3.kind in ("forward", "forward_modified")
        assert out3.audit_event == "yes_grant_bypass"

    def test_audited_on_both_ends(self, tmp_cfg):
        sid = "sess-grant-audit"
        evaluate(_runaway("a"), "claude-opus-4-7", sid, {}, config=tmp_cfg)
        evaluate(_opus_body("yes"), "claude-opus-4-7", sid, {}, config=tmp_cfg)
        evaluate(_runaway("b"), "claude-opus-4-7", sid, {}, config=tmp_cfg)
        ev = _events(tmp_cfg, sid)
        assert "yes_grant_created" in ev   # W2 write end
        assert "yes_grant_bypass" in ev    # W2 redemption end


# --------------------------------------------------------------------------
# W1 / W6: composite-key scope; cross-session + cross-agent non-reuse
# --------------------------------------------------------------------------

class TestCompositeKeyScope:
    def test_grant_hits_only_exact_key(self, tmp_path):
        store = GrantStore(str(tmp_path / "g.db"))
        store.create(session_id="s1", fleet_id="f1", agent_id="a1",
                     ttl_seconds=300, now=1000.0)
        # exact key → live
        assert store.get_active("s1", "f1", "a1", now=1100.0) is not None
        # W6: different session / fleet / agent all miss
        assert store.get_active("s2", "f1", "a1", now=1100.0) is None
        assert store.get_active("s1", "f2", "a1", now=1100.0) is None
        assert store.get_active("s1", "f1", "a2", now=1100.0) is None

    def test_redeem_cross_session_is_no_grant(self, tmp_path):
        store = GrantStore(str(tmp_path / "g.db"))
        store.create(session_id="s1", fleet_id="f1", agent_id="a1",
                     ttl_seconds=300, now=1000.0)
        status, _ = store.redeem("leaked", "f1", "a1", 0.10, now=1100.0)
        assert status == NO_GRANT


# --------------------------------------------------------------------------
# W7: strict TTL expiry — dead the instant now >= expires_at
# --------------------------------------------------------------------------

class TestTTLExpiry:
    def test_get_active_strict_boundary(self, tmp_path):
        store = GrantStore(str(tmp_path / "g.db"))
        store.create(session_id="s", fleet_id="", agent_id="a",
                     ttl_seconds=300, now=1000.0)  # expires_at == 1300
        assert store.get_active("s", "", "a", now=1299.999) is not None
        assert store.get_active("s", "", "a", now=1300.0) is None

    def test_redeem_after_expiry_deletes_and_reports_expired(self, tmp_path):
        store = GrantStore(str(tmp_path / "g.db"))
        store.create(session_id="s", fleet_id="", agent_id="a",
                     ttl_seconds=300, now=1000.0)
        status, _ = store.redeem("s", "", "a", 0.10, now=1300.0)
        assert status == EXPIRED
        # Row is gone — a second redeem sees nothing.
        status2, _ = store.redeem("s", "", "a", 0.10, now=1300.0)
        assert status2 == NO_GRANT


# --------------------------------------------------------------------------
# W4: dollar-budget clamp — decrement, exhaust without over-run
# --------------------------------------------------------------------------

class TestDollarClamp:
    def test_budget_decrements_then_exhausts(self, tmp_path):
        store = GrantStore(str(tmp_path / "g.db"))
        store.create(session_id="s", fleet_id="", agent_id="a",
                     ttl_seconds=300, max_cost_usd=1.0, now=1000.0)
        # $0.40 redeemed → $0.60 remains
        status, grant = store.redeem("s", "", "a", 0.40, now=1001.0)
        assert status == REDEEMED
        assert grant.max_cost_usd_remaining == pytest.approx(0.60)
        # $0.70 cannot be fully covered by $0.60 → spend out, no over-run.
        status2, _ = store.redeem("s", "", "a", 0.70, now=1002.0)
        assert status2 == EXHAUSTED
        # Grant is gone — the next request re-prompts.
        status3, _ = store.redeem("s", "", "a", 0.01, now=1003.0)
        assert status3 == NO_GRANT

    def test_exact_drain_deletes_row(self, tmp_path):
        store = GrantStore(str(tmp_path / "g.db"))
        store.create(session_id="s", fleet_id="", agent_id="a",
                     ttl_seconds=300, max_cost_usd=0.50, now=1000.0)
        status, grant = store.redeem("s", "", "a", 0.50, now=1001.0)
        assert status == REDEEMED
        assert grant.max_cost_usd_remaining == pytest.approx(0.0)
        # Drained to zero → row deleted, next request re-prompts.
        status2, _ = store.redeem("s", "", "a", 0.01, now=1002.0)
        assert status2 == NO_GRANT


# --------------------------------------------------------------------------
# W3: grant-table read error fails CLOSED to the block band
# --------------------------------------------------------------------------

class TestFailClosed:
    def test_redeem_error_falls_through_to_block(self, tmp_cfg, monkeypatch):
        sid = "sess-failclosed"
        evaluate(_runaway("a"), "claude-opus-4-7", sid, {}, config=tmp_cfg)
        evaluate(_opus_body("yes"), "claude-opus-4-7", sid, {}, config=tmp_cfg)

        def _boom(*args, **kwargs):
            raise RuntimeError("sqlite is unhappy")

        monkeypatch.setattr(grants_mod.GrantStore, "redeem", _boom)
        out = evaluate(_runaway("b"), "claude-opus-4-7", sid, {}, config=tmp_cfg)
        # Never auto-allows on a read error.
        assert out.kind == "block"
        assert "yes_grant_read_error" in _events(tmp_cfg, sid)


# --------------------------------------------------------------------------
# W5: hard-block band is non-bypassable by a grant
# --------------------------------------------------------------------------

class TestHardBlockNonBypassable:
    def test_active_grant_does_not_cover_hard_block(self, tmp_cfg):
        sid = "sess-hardblock"
        evaluate(_runaway("a"), "claude-opus-4-7", sid, {}, config=tmp_cfg)
        evaluate(_opus_body("yes"), "claude-opus-4-7", sid, {}, config=tmp_cfg)
        # 1M+ tokens → hard-block band, even with an active grant + bypass TIP.
        hard = _opus_body("[TIP: bypass=on] " + "x" * 4_000_000, max_tokens=50_000)
        out = evaluate(hard, "claude-opus-4-7", sid, {}, config=tmp_cfg)
        assert out.kind == "hard_block"

    def test_yes_grant_covers_rolling_caps_defaults_off(self):
        assert SpendGuardConfig().yes_grant_covers_rolling_caps is False


# --------------------------------------------------------------------------
# Acceptance (d): a NEGATIVE intent inside the window tears the grant down
# --------------------------------------------------------------------------

class TestNegativeDiscardsGrant:
    def test_no_after_yes_discards_grant(self, tmp_cfg):
        sid = "sess-negative"
        evaluate(_runaway("a"), "claude-opus-4-7", sid, {}, config=tmp_cfg)
        evaluate(_opus_body("yes"), "claude-opus-4-7", sid, {}, config=tmp_cfg)
        # No pending in flight; a bare "no" still discards the active grant.
        out_no = evaluate(_opus_body("no"), "claude-opus-4-7", sid, {}, config=tmp_cfg)
        assert out_no.kind == "forward"  # small request itself forwards
        assert "yes_grant_discarded" in _events(tmp_cfg, sid)
        # Grant gone → a fresh runaway blocks again.
        out_block = evaluate(_runaway("c"), "claude-opus-4-7", sid, {}, config=tmp_cfg)
        assert out_block.kind == "block"


# --------------------------------------------------------------------------
# Backwards-compat: [TIP: allow=once] keeps single-request semantics
# --------------------------------------------------------------------------

class TestAllowOnceNoGrant:
    def test_allow_once_opens_no_grant(self, tmp_cfg):
        sid = "sess-allow-once"
        evaluate(_runaway("a"), "claude-opus-4-7", sid, {}, config=tmp_cfg)
        # Approve the held request for a single shot only.
        evaluate(_opus_body("[TIP: allow=once max=$15] yes"),
                 "claude-opus-4-7", sid, {}, config=tmp_cfg)
        assert "yes_grant_created" not in _events(tmp_cfg, sid)
        # No grant → the next runaway blocks (single-request semantics held).
        out = evaluate(_runaway("b"), "claude-opus-4-7", sid, {}, config=tmp_cfg)
        assert out.kind == "block"
