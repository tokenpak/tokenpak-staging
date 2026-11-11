# SPDX-License-Identifier: Apache-2.0
"""Pre-send extension call site (contract ``tokenpak-presend/1``).

An installed extension may register one entry point in the
``tokenpak.presend`` group. The proxy calls its ``presend_hook`` once per
model request, after the Spend Guard has allowed the request and before the
upstream call, and its ``observe_response`` once the response usage is known.
OSS informs and guards; the extension calculates and decides.

OSS keeps every guarantee on its side of the boundary:

* With no extension installed, or ``TOKENPAK_PRESEND_HOOK=0``, nothing runs
  and the request is forwarded exactly as before.
* The extension runs on a small worker pool with a hard timeout
  (``TOKENPAK_PRESEND_TIMEOUT_MS``, default 50). An error, a timeout or a
  malformed result forwards the original bytes.
* The extension never returns bytes. It names routing values, and the proxy
  writes them into the same request by replacing only the JSON value of the
  top-level ``model`` and of the effort field already present
  (``output_config.effort``, ``reasoning.effort`` or a top-level ``effort``).
  Every other byte is preserved by construction; a body where those fields
  cannot be located exactly is forwarded unchanged.
* A rewritten request is checked again by the Spend Guard policy, without
  side effects, against the new model's context window and the same
  configuration. A rewrite the guard would block is refused, so a reroute
  never crosses a stop.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as _FutureTimeout
from dataclasses import dataclass
from json.decoder import scanstring
from typing import Any, Callable, Mapping, Optional

logger = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "tokenpak.presend"
CONTRACT = "tokenpak-presend/1"
_ENABLE_ENV = "TOKENPAK_PRESEND_HOOK"
_TIMEOUT_ENV = "TOKENPAK_PRESEND_TIMEOUT_MS"
_DEFAULT_TIMEOUT_MS = 50.0

_EFFORT_CONTAINERS = ("output_config", "reasoning")
_EFFORT_LABELS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"})
_WHITESPACE = " \t\n\r"


@dataclass(frozen=True)
class PresendResult:
    """What the proxy forwards. ``body`` is the original object when unchanged."""

    body: bytes
    rewritten: bool
    model: Optional[str]
    token: Optional[str]
    decision: Optional[str] = None
    revert_reason: Optional[str] = None


@dataclass(frozen=True)
class _Hooks:
    presend: Callable[[Mapping[str, Any]], Mapping[str, Any]]
    observe: Optional[Callable[[Mapping[str, Any]], None]]
    warm_up: Optional[Callable[[], None]] = None


_lock = threading.Lock()
_loaded = False
_hooks: Optional[_Hooks] = None
_pool: Optional[ThreadPoolExecutor] = None


def _discover() -> Optional[_Hooks]:
    from importlib.metadata import entry_points

    for entry in entry_points(group=ENTRY_POINT_GROUP):
        try:
            target = entry.load()
        except Exception as exc:  # a broken extension must not break the proxy
            logger.warning("tokenpak presend: entry point %s failed to load: %s", entry.name, exc)
            continue
        presend = getattr(target, "presend_hook", None)
        if callable(presend):
            observe = getattr(target, "observe_response", None)
            warm = getattr(target, "warm_up", None)
            return _Hooks(
                presend,
                observe if callable(observe) else None,
                warm if callable(warm) else None,
            )
    return None


def hooks() -> Optional[_Hooks]:
    """The registered extension, discovered once per process."""

    global _loaded, _hooks, _pool
    if os.environ.get(_ENABLE_ENV, "1").strip() == "0":
        return None
    if not _loaded:
        with _lock:
            if not _loaded:
                try:
                    _hooks = _discover()
                except Exception as exc:
                    logger.warning("tokenpak presend: discovery failed: %s", exc)
                    _hooks = None
                if _hooks is not None:
                    _pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="tokenpak-presend")
                    if _hooks.warm_up is not None:
                        # One-time extension loads run off the request path.
                        _pool.submit(_hooks.warm_up)
                _loaded = True
    return _hooks


def _reset_for_tests(new_hooks: Optional[_Hooks] = None) -> None:
    """Replace the discovered extension (tests only)."""

    global _loaded, _hooks, _pool
    with _lock:
        _hooks = new_hooks
        _loaded = True
        if new_hooks is not None and _pool is None:
            _pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="tokenpak-presend")


def _timeout_seconds() -> float:
    try:
        value = float(os.environ.get(_TIMEOUT_ENV, _DEFAULT_TIMEOUT_MS))
    except ValueError:
        value = _DEFAULT_TIMEOUT_MS
    return max(1.0, value) / 1000.0


class RoutingRewriteError(ValueError):
    """The routing fields cannot be located exactly; forward unchanged."""


def _skip_ws(text: str, index: int) -> int:
    while index < len(text) and text[index] in _WHITESPACE:
        index += 1
    return index


def _member_spans(text: str, start: int) -> tuple[dict[str, tuple[int, int]], int]:
    """Value spans of the JSON object at ``start``; duplicate keys refuse."""

    decoder = json.JSONDecoder()
    index = _skip_ws(text, start)
    if index >= len(text) or text[index] != "{":
        raise RoutingRewriteError("expected a JSON object")
    index = _skip_ws(text, index + 1)
    spans: dict[str, tuple[int, int]] = {}
    if index < len(text) and text[index] == "}":
        return spans, index + 1
    while True:
        if index >= len(text) or text[index] != '"':
            raise RoutingRewriteError("expected an object key")
        key, index = scanstring(text, index + 1)
        index = _skip_ws(text, index)
        if index >= len(text) or text[index] != ":":
            raise RoutingRewriteError("expected ':'")
        value_start = _skip_ws(text, index + 1)
        _, value_end = decoder.raw_decode(text, value_start)
        if key in spans:
            raise RoutingRewriteError(f"duplicate key {key!r}")
        spans[key] = (value_start, value_end)
        index = _skip_ws(text, value_end)
        if index < len(text) and text[index] == ",":
            index = _skip_ws(text, index + 1)
            continue
        if index < len(text) and text[index] == "}":
            return spans, index + 1
        raise RoutingRewriteError("malformed JSON object")


def apply_routing_fields(body: bytes, *, model: Optional[str], effort: Optional[str]) -> bytes:
    """Replace only the ``model`` and existing effort values of ``body``."""

    try:
        text = body.decode("utf-8")
        top, end = _member_spans(text, 0)
    except (UnicodeDecodeError, ValueError, IndexError) as exc:
        raise RoutingRewriteError(str(exc)) from exc
    if text[end:].strip(_WHITESPACE):
        raise RoutingRewriteError("trailing data after the JSON object")
    edits = []
    if model is not None:
        if not isinstance(model, str) or not model or "model" not in top:
            raise RoutingRewriteError("no model to rewrite")
        start, stop = top["model"]
        if text[start] != '"':
            raise RoutingRewriteError("model is not a string")
        edits.append((start, stop, json.dumps(model)))
    if effort is not None:
        if effort not in _EFFORT_LABELS:
            raise RoutingRewriteError(f"unrecognized effort {effort!r}")
        located = None
        for outer in _EFFORT_CONTAINERS:
            if outer in top:
                try:
                    members, _ = _member_spans(text, top[outer][0])
                except (ValueError, IndexError) as exc:
                    raise RoutingRewriteError(str(exc)) from exc
                if "effort" in members:
                    located = members["effort"]
                    break
        if located is None and "effort" in top:
            located = top["effort"]
        if located is None or text[located[0]] != '"':
            raise RoutingRewriteError("no effort field to rewrite")
        edits.append((located[0], located[1], json.dumps(effort)))
    if not edits:
        raise RoutingRewriteError("nothing to rewrite")
    for start, stop, replacement in sorted(edits, reverse=True):
        text = text[:start] + replacement + text[stop:]
    return text.encode("utf-8")


def guard_allows(body: bytes, model: str, config: Any = None) -> bool:
    """Side-effect-free Spend Guard policy check of a rewritten request."""

    from tokenpak.proxy.spend_guard._context_window import get_model_max_context
    from tokenpak.proxy.spend_guard.estimator import estimate
    from tokenpak.proxy.spend_guard.policy import decide, load_config

    cfg = config if config is not None else load_config()
    if getattr(cfg, "enabled", True) is False:
        return True
    try:
        window = get_model_max_context(model)
    except Exception:
        window = None
    verdict = decide(estimate(body, model), cfg, model_max_context_tokens=window)
    return verdict.decision in ("allow", "warn")


def run_presend(
    *,
    token: str,
    path: str,
    body: bytes,
    headers: Mapping[str, str],
    session_id: str,
    agent_id: str,
    guard_state: str,
    guard_config: Any = None,
    parsed: Optional[Mapping[str, Any]] = None,
) -> PresendResult:
    """Call the extension once. Never raises; failures forward ``body`` unchanged."""

    unchanged = PresendResult(body=body, rewritten=False, model=None, token=None)
    registered = hooks()
    if registered is None or not body:
        return unchanged
    request = {
        "contract": CONTRACT,
        "request_id": token,
        "path": path,
        "body": body,
        "headers": {str(k).lower(): str(v) for k, v in headers.items()},
        "session_id": session_id or None,
        "agent_id": agent_id or None,
        "guard": {"state": guard_state, "hard_stop": False},
    }
    if parsed is not None:
        request["parsed"] = parsed  # the proxy's own parse of these exact bytes; read only
    try:
        future = _pool.submit(registered.presend, request)  # type: ignore[union-attr]
        result = future.result(timeout=_timeout_seconds())
    except _FutureTimeout:
        return PresendResult(body, False, None, token, None, "timeout")
    except Exception as exc:
        logger.debug("tokenpak presend: extension error (forwarding unchanged): %s", exc)
        return PresendResult(body, False, None, token, None, "error")
    if not isinstance(result, Mapping):
        return PresendResult(body, False, None, token, None, "malformed")
    decision = result.get("decision") if isinstance(result.get("decision"), str) else None
    if result.get("action") != "rewrite":
        return PresendResult(body, False, None, token, decision)
    routing = result.get("set")
    if not isinstance(routing, Mapping) or set(routing) - {"model", "effort"}:
        return PresendResult(body, False, None, token, decision, "malformed")
    try:
        new_body = apply_routing_fields(
            body, model=routing.get("model"), effort=routing.get("effort")
        )
    except RoutingRewriteError:
        return PresendResult(body, False, None, token, decision, "rewrite_refused")
    model = routing.get("model")
    if model is None:
        try:
            model = (parsed if parsed is not None else json.loads(body)).get("model")
        except (ValueError, AttributeError):
            model = None
    if not isinstance(model, str) or not model:
        return PresendResult(body, False, None, token, decision, "malformed")
    try:
        allowed = guard_allows(new_body, model, guard_config)
    except Exception as exc:
        logger.debug("tokenpak presend: guard re-check failed (forwarding unchanged): %s", exc)
        allowed = False
    if not allowed:
        return PresendResult(body, False, None, token, decision, "guard_recheck")
    return PresendResult(new_body, True, model, token, decision)


def run_observe(
    result: Optional[PresendResult],
    *,
    status: Optional[int],
    served_model: str,
    usage: Mapping[str, Optional[int]],
) -> None:
    """Report the response usage of a request the extension saw. Never raises."""

    registered = _hooks
    if result is None or result.token is None or registered is None or registered.observe is None:
        return
    try:
        registered.observe(
            {
                "request_id": result.token,
                "applied": result.rewritten,
                "revert_reason": result.revert_reason,
                "status": status,
                "served_model": served_model,
                "usage": dict(usage),
            }
        )
    except Exception as exc:
        logger.debug("tokenpak presend: observe failed: %s", exc)
