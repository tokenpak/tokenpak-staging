# SPDX-License-Identifier: Apache-2.0
"""In-flight request registry — facts about requests currently being served.

Process-local, in-memory, TTL-bounded — the same shape as
``spend_guard.rolling_caps._INFLIGHT`` (module-level dict + lock), not a new
architectural pattern. Populated at request start and torn down when the
request finishes (streaming or not); a crashed/never-finished request
self-expires via TTL so it cannot accumulate forever.

Facts only, no estimates: ``started_at``, time-to-first-byte, and a
live output-token count read straight off the wire via
``streaming.IncrementalUsageTracker``. No estimator math lives here — any
cost/token *projection* attached to a snapshot is read verbatim from the
existing rolling-caps admission ledger via
``spend_guard.rolling_caps.get_admitted_projection`` (already computed at
admission time, not recomputed here).
"""

from __future__ import annotations

import threading
import time
from typing import Optional

# Keyed by request id (server.py's existing per-request ``_req_id``).
# value: (model, started_at_epoch, ttfb_ms | None, output_tokens_live,
#         admission_ticket | None)
_INFLIGHT: dict[str, tuple[str, float, Optional[int], int, Optional[str]]] = {}
_LOCK = threading.Lock()

# Registry entries are torn down explicitly by the request that created them
# (see server.py finish() call in the do_POST finally-equivalent path). This
# TTL is a backstop only, for requests that crash before reaching that point.
_INFLIGHT_TTL_SEC = 3600.0


def register(
    request_id: str,
    *,
    model: str,
    started_at: Optional[float] = None,
    admission_ticket: Optional[str] = None,
) -> None:
    """Record that ``request_id`` has started.

    ``started_at`` should be the caller's existing per-request t0 anchor
    (epoch seconds) when one is available, so elapsed/ttfb figures agree
    with the request's actual start rather than the moment registration
    happened to run; defaults to "now" otherwise.
    """
    if not request_id:
        return
    now = time.time()
    with _LOCK:
        _sweep_expired_locked(now)
        _INFLIGHT[request_id] = (model or "", started_at or now, None, 0, admission_ticket)


def mark_ttfb(request_id: str) -> Optional[int]:
    """Record time-to-first-byte for ``request_id`` if not already set.

    Idempotent — only the first call after ``register`` has an effect;
    subsequent calls (e.g. later chunks) are no-ops and return the value
    already recorded. Returns ``None`` if the request was never registered
    (e.g. registry disabled, or called out of order) or on the "already set"
    path when no timestamp is available.
    """
    with _LOCK:
        entry = _INFLIGHT.get(request_id)
        if entry is None:
            return None
        model, started_at, ttfb_ms, output_tokens, ticket = entry
        if ttfb_ms is not None:
            return ttfb_ms
        ttfb_ms = int((time.time() - started_at) * 1000)
        _INFLIGHT[request_id] = (model, started_at, ttfb_ms, output_tokens, ticket)
        return ttfb_ms


def update_output_tokens(request_id: str, output_tokens: int) -> None:
    """Update the live output-token count for ``request_id``."""
    with _LOCK:
        entry = _INFLIGHT.get(request_id)
        if entry is None:
            return
        model, started_at, ttfb_ms, previous, ticket = entry
        _INFLIGHT[request_id] = (
            model,
            started_at,
            ttfb_ms,
            max(previous, output_tokens),
            ticket,
        )


def finish(request_id: str) -> None:
    """Remove ``request_id`` from the registry. Idempotent."""
    with _LOCK:
        _INFLIGHT.pop(request_id, None)


def _sweep_expired_locked(now: float) -> None:
    cutoff = now - _INFLIGHT_TTL_SEC
    expired = [
        rid for rid, (_m, started_at, _t, _o, _tk) in _INFLIGHT.items() if started_at < cutoff
    ]
    for rid in expired:
        _INFLIGHT.pop(rid, None)


def snapshot() -> list[dict[str, object]]:
    """Read-only list of currently admitted in-flight requests.

    Each entry reports facts only: elapsed time since ``started_at``, time-
    to-first-byte (once observed), and the live output-token count. When the
    request has an associated rolling-caps admission ticket, the projected
    cost/tokens already computed at admission time are attached verbatim
    (no recomputation here) via
    ``spend_guard.rolling_caps.get_admitted_projection``.
    """
    from tokenpak.proxy.spend_guard.rolling_caps import get_admitted_projection

    now = time.time()
    cutoff = now - _INFLIGHT_TTL_SEC
    with _LOCK:
        items = [
            (request_id, entry) for request_id, entry in _INFLIGHT.items() if entry[1] >= cutoff
        ]

    result = []
    for request_id, (model, started_at, ttfb_ms, output_tokens, ticket) in items:
        entry = {
            "request_id": request_id,
            "model": model,
            "started_at": started_at,
            "elapsed_ms": int((now - started_at) * 1000),
            "ttfb_ms": ttfb_ms,
            "output_tokens_live": output_tokens,
        }
        projection = get_admitted_projection(ticket) if ticket else None
        if projection is not None:
            entry["projected_cost_usd"] = projection["projected_cost_usd"]
            entry["projected_tokens_total"] = projection["projected_tokens_total"]
        result.append(entry)
    return result


def reset_for_testing() -> None:
    """Test-only — clear all registry state."""
    with _LOCK:
        _INFLIGHT.clear()
