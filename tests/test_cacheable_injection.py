"""cacheable-injection retargeting — proxy-route two-layer injection.

Covers the packet's acceptance criteria at the adapter contract level:
  - stable layer is cacheable, volatile layer is not (AC 1/2/3)
  - stable serialization is deterministic (AC 4)
  - stable hash is stable across turns; volatile churn does not move it (AC 5/6)
  - Claude Code / client-managed (explicit TTL) path is untouched, no proxy
    cache_control added, attribution stays client (AC 7/8/9/10)
  - cache_origin ∈ {proxy, client, unknown} (attribution contract)
  - legacy positional call is byte-for-byte unchanged
"""

from __future__ import annotations

import json

from tokenpak.proxy.adapters.anthropic_adapter import AnthropicAdapter
from tokenpak.proxy.adapters.base import cacheable_layer_hash
from tokenpak.proxy.adapters.passthrough_adapter import PassthroughAdapter


def _body(system, *, ttl=False):
    msg_cc = {"type": "ephemeral", "ttl": "1h"} if ttl else None
    user_block = {"type": "text", "text": "hello"}
    if msg_cc:
        user_block["cache_control"] = msg_cc
    return json.dumps(
        {
            "model": "claude-x",
            "system": system,
            "messages": [{"role": "user", "content": [user_block]}],
        }
    ).encode()


def _system_blocks(result_bytes):
    data = json.loads(result_bytes)
    sysv = data.get("system")
    return sysv


A = AnthropicAdapter()


# ---- AC 1/2/3: mixed stable + volatile placement and cache markers ----
def test_mixed_stable_cacheable_volatile_uncached():
    trace = {}
    out = A.inject_system_context(
        _body("ORIG SYSTEM"),
        stable_text="STABLE GLOSSARY",
        volatile_text="VOLATILE BM25",
        trace_out=trace,
    )
    blocks = _system_blocks(out)
    assert isinstance(blocks, list)
    texts = [b["text"] for b in blocks]
    assert texts == ["ORIG SYSTEM", "STABLE GLOSSARY", "VOLATILE BM25"]
    # original + stable carry cache_control; volatile must NOT.
    by_text = {b["text"]: b for b in blocks}
    assert "cache_control" in by_text["ORIG SYSTEM"]
    assert "cache_control" in by_text["STABLE GLOSSARY"]
    assert "cache_control" not in by_text["VOLATILE BM25"]
    assert trace["cache_origin"] == "proxy"
    assert trace["stable_present"] and trace["volatile_present"]


# ---- stable-only ----
def test_stable_only_is_cacheable_no_volatile_block():
    trace = {}
    out = A.inject_system_context(
        _body("ORIG"), stable_text="STABLE", volatile_text="", trace_out=trace
    )
    blocks = _system_blocks(out)
    texts = [b["text"] for b in blocks]
    assert texts == ["ORIG", "STABLE"]
    assert "cache_control" in {b["text"]: b for b in blocks}["STABLE"]
    assert trace["cache_origin"] == "proxy"
    assert trace["stable_present"] and not trace["volatile_present"]


# ---- volatile-only via keywords ----
def test_volatile_only_keyword_uncached():
    trace = {}
    out = A.inject_system_context(
        _body("ORIG"), stable_text="", volatile_text="VOL", trace_out=trace
    )
    blocks = _system_blocks(out)
    by_text = {b["text"]: b for b in blocks}
    assert "VOL" in by_text
    assert "cache_control" not in by_text["VOL"]
    # original system still cached → proxy-attributable
    assert "cache_control" in by_text["ORIG"]
    assert trace["cache_origin"] == "proxy"
    assert trace["volatile_present"] and not trace["stable_present"]


# ---- AC 7/8/9/10: client explicit-TTL path is NOT given proxy cache_control ----
def test_client_explicit_ttl_not_touched_attribution_client():
    trace = {}
    out = A.inject_system_context(
        _body("ORIG", ttl=True),
        stable_text="STABLE",
        volatile_text="VOL",
        trace_out=trace,
    )
    blocks = _system_blocks(out)
    by_text = {b["text"]: b for b in blocks}
    # We must add NO new cache_control on the client-managed path.
    assert "cache_control" not in by_text.get("ORIG", {})
    assert "cache_control" not in by_text.get("STABLE", {})
    assert "cache_control" not in by_text.get("VOL", {})
    assert trace["cache_origin"] == "client"


# ---- AC 4: deterministic stable serialization ----
def test_stable_serialization_deterministic():
    o1 = A.inject_system_context(_body("ORIG"), stable_text="S", volatile_text="V1")
    o2 = A.inject_system_context(_body("ORIG"), stable_text="S", volatile_text="V1")
    assert o1 == o2


# ---- AC 5/6: volatile churn does not move the stable cached prefix ----
def test_volatile_change_does_not_churn_stable_prefix():
    t1, t2 = {}, {}
    o1 = A.inject_system_context(
        _body("ORIG"), stable_text="S", volatile_text="turn-1", trace_out=t1
    )
    o2 = A.inject_system_context(
        _body("ORIG"), stable_text="S", volatile_text="turn-2-different", trace_out=t2
    )
    b1 = _system_blocks(o1)
    b2 = _system_blocks(o2)
    # Cached prefix = everything up to and including the stable block (index 0,1).
    assert b1[:2] == b2[:2]
    # Stable hash unchanged; volatile hash differs.
    assert t1["stable_hash"] == t2["stable_hash"]
    assert t1["volatile_hash"] != t2["volatile_hash"]
    # The stable block still carries the breakpoint.
    assert "cache_control" in b1[1]


def test_stable_hash_helper_stable_and_distinct():
    assert cacheable_layer_hash("S") == cacheable_layer_hash("S")
    assert cacheable_layer_hash("S") != cacheable_layer_hash("S2")
    assert cacheable_layer_hash("") == ""


# ---- AC 7/8: passthrough (Claude Code byte-preserved) is a no-op, never overclaims ----
def test_passthrough_no_touch_attribution_not_proxy():
    p = PassthroughAdapter()
    body = _body("ORIG")
    trace = {}
    out = p.inject_system_context(
        body, stable_text="S", volatile_text="V", trace_out=trace
    )
    assert out == body  # byte-identical: no injection
    assert trace["cache_origin"] == "client"  # never "proxy" on passthrough


# ---- legacy positional call unchanged (back-compat) ----
def test_legacy_positional_single_block_volatile():
    trace = {}
    out = A.inject_system_context(_body("ORIG"), "legacy injection", trace_out=trace)
    blocks = _system_blocks(out)
    by_text = {b["text"]: b for b in blocks}
    assert "legacy injection" in by_text
    assert "cache_control" not in by_text["legacy injection"]  # injected = volatile
    assert "cache_control" in by_text["ORIG"]  # original stays cached
