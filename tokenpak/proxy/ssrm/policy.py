"""SSRM Phase 1 policy — rule-based decision engine (instrumentation-only).

Computes one of six actions per request following the parent §5 decision
model with Amendment A1 §A1.3 (effective-context ladder). The action is
ADVISORY in Phase 1: callers MUST NOT branch on it for request routing.
Phase 2+ will switch ``advisory_only`` to False for selected actions and
the proxy will start honoring them.

Tie-break rule (parent §5): higher-severity action wins. We compute every
action that fires and return the highest-severity one.
"""

from __future__ import annotations

import os
import threading
from typing import Optional

from .audit import record_decision
from .contracts import (
    ACTION_BLOCK,
    ACTION_COMPRESS,
    ACTION_CONTINUE,
    ACTION_QUARANTINE,
    ACTION_ROTATE,
    ACTION_SEVERITY,
    ACTION_WARN,
    Decision,
    Signals,
)
from .signals import compute_signals


# Default thresholds (parent §6 + Amendment A1 §A1.2). All Phase 1
# defaults; production values come from ~/.tokenpak/config.yaml `ssrm:`
# block when available.
DEFAULT_THRESHOLDS: dict = {
    "effective_context_pct_warn":     70.0,
    "effective_context_pct_compress": 80.0,
    "effective_context_pct_block":    85.0,
    "effective_context_pct_hard":     90.0,
    "context_pct_hard_stop":          100.0,
    "cache_read_amplification_warn":   5.0,
    "cache_read_amplification_act":   10.0,
    "relevance_drift_warn":            0.4,
    "session_age_drift_min_turns":    20,
    "fingerprint_repeat_quarantine":   3,
    "rotation_savings_required_pct":  40,
    "context_pct_projected_next_warn": 80.0,
}


_CONFIG_LOCK = threading.Lock()
_CONFIG_CACHE: Optional[dict] = None


def _read_config_yaml() -> dict:
    """Read ~/.tokenpak/config.yaml (best-effort)."""
    global _CONFIG_CACHE
    with _CONFIG_LOCK:
        if _CONFIG_CACHE is not None:
            return _CONFIG_CACHE
        cfg: dict = {}
        path = os.path.expanduser("~/.tokenpak/config.yaml")
        if os.path.isfile(path):
            try:
                import yaml  # type: ignore
                with open(path) as f:
                    cfg = yaml.safe_load(f) or {}
            except Exception:
                cfg = {}
        _CONFIG_CACHE = cfg
        return cfg


def reset_config_cache() -> None:
    """Test-only — force config to be reloaded on next access."""
    global _CONFIG_CACHE
    with _CONFIG_LOCK:
        _CONFIG_CACHE = None


def _get_ssrm_config() -> dict:
    """Return SSRM config block with defaults merged in.

    Order: env vars `TOKENPAK_SSRM_*` > config.yaml `ssrm:` block > defaults.
    Phase 1 default `ssrm.enabled = false`.
    """
    cfg = (_read_config_yaml() or {}).get("ssrm") or {}
    merged: dict = {
        "enabled": False,
        "audit_db_path": "~/.tokenpak/ssrm_audit.db",
        "state_db_path": "~/.tokenpak/ssrm_state.db",
        "fingerprint_ttl_seconds": 7200,
        "drift_algorithm": "jaccard_word_set",
    }
    for k, v in DEFAULT_THRESHOLDS.items():
        merged[k] = v
    for k, v in (cfg or {}).items():
        merged[k] = v
    # Env-var overrides
    for k in list(merged.keys()):
        env_key = f"TOKENPAK_SSRM_{k.upper()}"
        if env_key in os.environ:
            raw = os.environ[env_key]
            try:
                if isinstance(merged[k], bool):
                    merged[k] = raw.strip().lower() in ("1", "true", "yes", "on")
                elif isinstance(merged[k], int):
                    merged[k] = int(raw)
                elif isinstance(merged[k], float):
                    merged[k] = float(raw)
                else:
                    merged[k] = raw
            except (ValueError, TypeError):
                pass
    return merged


def _classify(signals: Signals, thresholds: dict) -> tuple[str, str]:
    """Apply the parent §5 decision rules (with A1 amendments) to signals.

    Returns (action, reason). Reason is a short human-readable string for
    the audit row.
    """
    eff = signals.effective_context_pct
    inp_pct = signals.context_pct_now
    crr = signals.cache_read_ratio
    drift = signals.relevance_drift_score
    age = signals.session_age_turns
    fp_repeat = signals.prompt_fingerprint_repeat_count
    progress = signals.progress_signal
    nxt = signals.context_pct_projected_next

    candidates: list[tuple[str, str]] = []

    # Rule 1: hard stop (immutable; input-only basis as per parent §5 step 1)
    if inp_pct >= thresholds["context_pct_hard_stop"]:
        candidates.append((ACTION_BLOCK, f"hard_stop input_pct={inp_pct:.1f}>=100"))

    # Rule 2: stop-loss quarantine
    if progress == "no_progress" and fp_repeat >= int(thresholds["fingerprint_repeat_quarantine"]):
        candidates.append(
            (ACTION_QUARANTINE,
             f"no_progress+fingerprint_repeat={fp_repeat}>="
             f"{int(thresholds['fingerprint_repeat_quarantine'])}")
        )

    # Rule 3a-c: effective-context ladder (A1)
    if eff >= thresholds["effective_context_pct_hard"]:
        # OSS treats as block_with_bypass; Pro would rotate. Phase 1 OSS = block.
        candidates.append((ACTION_BLOCK, f"eff_ctx={eff:.1f}>={thresholds['effective_context_pct_hard']}"))
    elif eff >= thresholds["effective_context_pct_block"]:
        candidates.append((ACTION_BLOCK, f"eff_ctx={eff:.1f}>={thresholds['effective_context_pct_block']}"))
    elif eff >= thresholds["effective_context_pct_compress"]:
        candidates.append((ACTION_COMPRESS, f"eff_ctx={eff:.1f}>={thresholds['effective_context_pct_compress']}"))
    elif eff >= thresholds["effective_context_pct_warn"]:
        candidates.append((ACTION_WARN, f"eff_ctx={eff:.1f}>={thresholds['effective_context_pct_warn']}"))

    # Rule 4: cache-read amplification (overrides low effective % if extreme)
    if crr >= thresholds["cache_read_amplification_act"]:
        candidates.append((ACTION_COMPRESS, f"cache_read_ratio={crr:.2f}>={thresholds['cache_read_amplification_act']}"))
    elif crr >= thresholds["cache_read_amplification_warn"]:
        candidates.append((ACTION_WARN, f"cache_read_ratio={crr:.2f}>={thresholds['cache_read_amplification_warn']}"))

    # Rule 5: relevance drift on aged session
    if drift < thresholds["relevance_drift_warn"] and age > int(thresholds["session_age_drift_min_turns"]):
        # OSS Phase 1 — would-be compress_or_recap (Pro could rotate; OSS suggests compress).
        candidates.append((ACTION_COMPRESS, f"drift={drift:.2f}<{thresholds['relevance_drift_warn']} age={age}"))

    # Rule 6: projected next-request context pressure (only when eff_ctx is still moderate)
    if nxt >= thresholds["context_pct_projected_next_warn"] and eff < thresholds["effective_context_pct_warn"]:
        candidates.append((ACTION_WARN, f"projected_next={nxt:.1f}>={thresholds['context_pct_projected_next_warn']}"))

    if not candidates:
        return ACTION_CONTINUE, "no rule fired"

    # Highest-severity tie-break wins
    candidates.sort(key=lambda c: ACTION_SEVERITY.get(c[0], 0), reverse=True)
    return candidates[0]


def decide(
    body: bytes | str | dict | None,
    model: str,
    session_id: str,
    headers: dict | None = None,
    *,
    persist: bool = True,
) -> Decision:
    """Top-level SSRM decision call.

    Computes signals, applies the rule engine, optionally writes the
    decision to ssrm_audit.db, and returns the Decision object. The
    proxy MUST NOT branch on `decision.action` in Phase 1.

    Args:
        body: request body (bytes / str / dict)
        model: model id (e.g. "claude-opus-4-7")
        session_id: resolved session id from the existing pipeline
        headers: request headers dict (used to extract agent_id)
        persist: if True, write the decision to ssrm_audit.db
    """
    config = _get_ssrm_config()

    # When SSRM is disabled, return a no-op continue decision and skip
    # persistence so production behavior remains unchanged.
    if not config.get("enabled"):
        return Decision(
            action=ACTION_CONTINUE,
            signals=Signals(model=model, session_id=session_id or ""),
            reason="ssrm.enabled=false",
            advisory_only=True,
        )

    signals = compute_signals(
        body,
        model,
        session_id,
        headers,
        state_db_path=config["state_db_path"],
        drift_algorithm=config.get("drift_algorithm", "jaccard_word_set"),
        fingerprint_ttl_seconds=int(config.get("fingerprint_ttl_seconds", 7200)),
    )
    action, reason = _classify(signals, config)
    decision = Decision(
        action=action,
        signals=signals,
        reason=reason,
        advisory_only=True,
    )
    if persist:
        try:
            record_decision(decision, audit_db_path=config["audit_db_path"])
        except Exception:
            # Never let audit-write failures break the request path.
            pass
    return decision
