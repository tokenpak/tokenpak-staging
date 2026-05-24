"""Intent-based routing policy.

Pro bridge: re-exports from tokenpak-pro when available.
Without tokenpak-pro, this module raises ImportError (caught by proxy_v4.py try/except).
"""
try:
    from tokenpak_pro.features.proxy.intent_policy import (
        decide,
        resolve_policy,
        DecisionAction,
        PolicyResult,
        RoutingDecision,
        apply_context_contract,
        is_known_intent,
        known_intents,
    )
except ImportError:
    raise ImportError(
        "intent_policy requires tokenpak-pro. "
        "Install with: pip install tokenpak-pro"
    )

__all__ = ["decide", "resolve_policy", "DecisionAction", "PolicyResult", "RoutingDecision", "apply_context_contract", "is_known_intent", "known_intents"]
