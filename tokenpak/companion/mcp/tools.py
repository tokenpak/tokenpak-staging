# SPDX-License-Identifier: Apache-2.0
"""MCP tool definitions and handlers.

Each tool is a (schema, handler) pair.  The server dispatches by name.
Adding a new tool = adding one entry to TOOLS + one handler function.

Design principle: tools are stateless functions that receive the shared
CompanionState and return a JSON-serializable result.  State mutation
goes through CompanionState methods so it's centralized and testable.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from tokenpak.companion import config as _companion_config

from ..config import CompanionConfig

# Default input-rate for cost estimation when no model hint is available.
# Matches pre_send.py's fallback (sonnet input rate). Kept local rather than
# imported so tools.py has no cross-module coupling with the hook.
_COMPANION_DEFAULT_INPUT_RATE_USD_PER_MTOK = 3.0


def current_session_id() -> str:
    """Read the live session id from the run-dir marker written by the
    pre_send hook (session binding). The MCP server is a separate process
    from the hook, so this file is the only channel by which it learns the
    active session id. Returns "" if no marker exists yet."""
    try:
        run_dir = _companion_config.journal_run_dir()
        marker = run_dir / "current-session"
        if marker.exists():
            return marker.read_text(encoding="utf-8").strip()
    except Exception:
        pass
    return ""


@dataclass
class CompanionState:
    """Shared mutable state for the MCP server process.

    Lives for the duration of the Claude Code session.  All tools receive
    this and can read/mutate it.
    """

    config: CompanionConfig = field(default_factory=CompanionConfig.from_env)
    call_count: int = 0
    session_id: str = ""
    transcript_path: str = ""

    # Lazy-initialized subsystems
    _budget_tracker: Any = None
    _journal_store: Any = None

    @property
    def budget_tracker(self) -> Any:
        if self._budget_tracker is None:
            from ..budget.tracker import BudgetTracker

            self._budget_tracker = BudgetTracker(
                db_path=self.config.journal_dir / "budget.db",
                daily_budget=self.config.budget_daily_usd,
            )
        return self._budget_tracker

    @property
    def journal_store(self) -> Any:
        if self._journal_store is None:
            from ..journal.store import JournalStore

            self._journal_store = JournalStore(
                db_path=self.config.journal_dir / "journal.db",
            )
        return self._journal_store


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------


@dataclass
class ToolDef:
    """MCP tool definition."""

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[CompanionState, dict[str, Any]], str]


def _handle_estimate_tokens(state: CompanionState, args: dict[str, Any]) -> str:
    """Estimate tokens via proxy /tpk/v1/tokens/estimate."""
    text = args.get("text", "")
    file_path = args.get("file_path", "")
    body: dict[str, Any] = {}
    if file_path:
        body["file_path"] = file_path
    elif text:
        body["text"] = text
    else:
        return json.dumps({"error": "provide text or file_path"})

    status, resp = _proxy_post("/tpk/v1/tokens/estimate", body)
    if status == 0:
        return json.dumps({"error": "proxy_unreachable", "detail": resp.get("detail", "")})
    if status >= 400:
        return json.dumps(resp)
    return json.dumps(resp)


def _handle_estimate_tokens_legacy_unused(state: CompanionState, args: dict[str, Any]) -> str:
    """Legacy in-process estimator kept for reference; no longer registered."""
    text = args.get("text", "")
    file_path = args.get("file_path", "")
    if file_path:
        p = Path(file_path)
        if p.exists():
            text = p.read_text(errors="replace")
        else:
            return json.dumps({"error": f"File not found: {file_path}"})

    chars = len(text)
    try:
        from tokenpak.telemetry.tokens import count_tokens

        CHUNK = 100_000
        if chars <= CHUNK:
            tokens = count_tokens(text)
        else:
            tokens = sum(count_tokens(text[i : i + CHUNK]) for i in range(0, chars, CHUNK))
        method = "tiktoken"
    except Exception:
        tokens = chars // 4
        method = "heuristic (chars/4)"

    return json.dumps(
        {
            "tokens": tokens,
            "chars": chars,
            "method": method,
            "source": file_path or "inline text",
        },
        indent=2,
    )


def _handle_check_budget(state: CompanionState, args: dict[str, Any]) -> str:
    """Check remaining budget via proxy /tpk/v1/budget."""
    status, body = _proxy_get("/tpk/v1/budget")
    if status == 0:
        return json.dumps(
            {
                "error": "proxy_unreachable",
                "detail": body.get("detail", "is the tokenpak proxy running?"),
            }
        )
    if status >= 400:
        return json.dumps(body)
    # Honest-reporting (split-brain trust fix): the proxy only accounts for
    # traffic actually routed through the TokenPak value plane. A client routed
    # natively (e.g. provider-native caching, no TokenPak proxy in path) is NOT
    # counted here — so a low/zero figure must never be read as authoritative
    # total spend. Always attach an explicit scope note; never return a bare 0.
    if isinstance(body, dict):
        body = dict(body)
        body["_tokenpak_scope"] = (
            "Reflects ONLY traffic routed through the TokenPak value plane "
            "(proxy/companion). Natively-routed client traffic is NOT counted "
            "here; a low or zero figure does not mean low total spend. If this "
            "client is not routed through TokenPak, treat TokenPak accounting "
            "as unavailable for it."
        )
    return json.dumps(body, indent=2)


def _handle_load_capsule(state: CompanionState, args: dict[str, Any]) -> str:
    """Load / list memory capsules via proxy /tpk/v1/capsules*."""
    session_id = str(args.get("session_id", "")).strip()
    if not session_id:
        status, body = _proxy_get("/tpk/v1/capsules", {"limit": 10})
        if status == 0:
            return json.dumps({"error": "proxy_unreachable", "detail": body.get("detail", "")})
        if status >= 400:
            return json.dumps(body)
        return json.dumps(body, indent=2)

    # Carry the CALLER's session_id so the proxy can attribute the
    # load_capsule savings event to the right session journal.
    params = {}
    if state.session_id:
        params["caller_session_id"] = state.session_id
    status, body = _proxy_get(
        f"/tpk/v1/capsules/{_url_parse.quote(session_id, safe='')}",
        params,
    )
    if status == 0:
        return json.dumps({"error": "proxy_unreachable", "detail": body.get("detail", "")})
    if status >= 400:
        return json.dumps(body)
    # Preserve the old behavior of returning the capsule CONTENT as a bare
    # string when a specific session was requested.
    if isinstance(body, dict) and "content" in body:
        return body["content"]
    return json.dumps(body)


def _handle_prune_context(state: CompanionState, args: dict[str, Any]) -> str:
    """Compress verbose content via proxy /tpk/v1/compress."""
    text = args.get("text", "")
    if not text:
        return json.dumps({"error": "No text provided"})
    body = {
        "text": text,
        "max_tokens": args.get("max_tokens", 2000),
    }
    if state.session_id:
        body["session_id"] = state.session_id  # proxy records savings to journal
    status, resp = _proxy_post("/tpk/v1/compress", body)
    if status == 0:
        return json.dumps({"error": "proxy_unreachable", "detail": resp.get("detail", "")})
    if status >= 400:
        return json.dumps(resp)
    return json.dumps(resp)


def _handle_journal_read(state: CompanionState, args: dict[str, Any]) -> str:
    """Read journal entries via proxy /tpk/v1/journal/*."""
    target = args.get("session_id") or state.session_id
    entry_type = args.get("entry_type")
    limit = args.get("limit", 20)

    if not target:
        # List recent sessions
        status, body = _proxy_get("/tpk/v1/journal/sessions", {"limit": 10})
    else:
        params: dict[str, Any] = {"limit": limit}
        if entry_type:
            params["entry_type"] = entry_type
        status, body = _proxy_get(
            f"/tpk/v1/journal/{_url_parse.quote(target, safe='')}",
            params,
        )

    if status == 0:
        return json.dumps(
            {
                "error": "proxy_unreachable",
                "detail": body.get("detail", ""),
            }
        )
    if status >= 400:
        return json.dumps(body)
    return json.dumps(body, indent=2)


def _handle_journal_write(state: CompanionState, args: dict[str, Any]) -> str:
    """Add a note to the current session journal via proxy POST."""
    content = args.get("content", "")
    if not content:
        return json.dumps({"error": "No content provided"})

    session_id = state.session_id
    if not session_id:
        return json.dumps({"error": "No active session"})

    status, body = _proxy_post(
        f"/tpk/v1/journal/{_url_parse.quote(session_id, safe='')}/entry",
        {"content": content, "entry_type": "user"},
    )
    if status == 0:
        return json.dumps(
            {
                "error": "proxy_unreachable",
                "detail": body.get("detail", ""),
            }
        )
    if status >= 400:
        return json.dumps(body)
    return json.dumps(body)


def _handle_session_info(state: CompanionState, args: dict[str, Any]) -> str:
    """Environment snapshot: merges local companion state with proxy status.

    Companion-local fields (session_id, call_count, config) stay in-process
    since they describe the companion's own process. Proxy-owned state
    (version, uptime, mode, cache_ttl, vault, session counters) comes from
    /tpk/v1/session/info. Proxy-down returns the local-only view with a
    "proxy" block indicating the degradation.
    """
    local = {
        "companion_version": "0.1.0",
        "session_id": state.session_id,
        "call_count": state.call_count,
        "config": {
            "profile": state.config.profile,
            "budget_daily_usd": state.config.budget_daily_usd,
            "hooks_enabled": state.config.hooks_enabled,
            "prune_threshold": state.config.prune_threshold,
            "memory_dirs": [str(p) for p in getattr(state.config, "memory_dirs", [])],
        },
    }
    # Make "no lessons" self-explaining: report whether a memory source is
    # configured at all (TOKENPAK_COMPANION_MEMORY_DIRS / vault), so a
    # fresh user knows why ingestion may be empty and how to point it at notes.
    if not local["config"]["memory_dirs"]:
        local["config"]["memory_source_hint"] = (
            "no memory dirs configured — set TOKENPAK_COMPANION_MEMORY_DIRS to "
            "directories of your own Markdown notes, then ingest them with the "
            "companion memory-source API (ingest_from_dir / ingest_sources)"
        )
    status, proxy_info = _proxy_get("/tpk/v1/session/info")
    if status == 200:
        local["proxy"] = proxy_info
    elif status == 0:
        local["proxy"] = {"error": "proxy_unreachable", "detail": proxy_info.get("detail", "")}
    else:
        local["proxy"] = proxy_info
    return json.dumps(local, indent=2)


# ---------------------------------------------------------------------------
# Proxy REST client — thin HTTP wrapper used by vault_* (and future) tools.
# Per 2026-04-17 architecture: proxy owns the state, companion calls it.
# ---------------------------------------------------------------------------

import os as _os
import urllib.error as _url_err
import urllib.parse as _url_parse
import urllib.request as _url_req


def _proxy_base_url() -> str:
    return _os.environ.get("TOKENPAK_PROXY_URL", "http://127.0.0.1:8766")


def _proxy_request(
    method: str,
    path: str,
    params: Optional[dict[str, Any]] = None,
    body: Optional[dict[str, Any]] = None,
) -> tuple[int, dict[str, Any]]:
    """HTTP call against the local proxy's /tpk/v1/* app API.

    Returns (status_code, json_body). Never raises — network/parse errors
    become (0, {"error": ..., "detail": ...}) so tool handlers can
    degrade gracefully.
    """
    url = _proxy_base_url().rstrip("/") + path
    if params:
        url = f"{url}?{_url_parse.urlencode({k: v for k, v in params.items() if v is not None})}"
    data: Optional[bytes] = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    req = _url_req.Request(url, method=method, data=data)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    key = _os.environ.get("TOKENPAK_PROXY_KEY", "").strip()
    if key:
        req.add_header("X-TPK-Key", key)
    try:
        with _url_req.urlopen(req, timeout=5.0) as resp:
            raw = resp.read()
            try:
                return resp.status, json.loads(raw.decode("utf-8"))
            except Exception as exc:
                return resp.status, {"error": "invalid_json", "detail": str(exc)}
    except _url_err.HTTPError as exc:
        try:
            parsed = json.loads(exc.read().decode("utf-8"))
        except Exception:
            parsed = {"error": f"http_{exc.code}", "detail": str(exc)}
        return exc.code, parsed
    except Exception as exc:
        return 0, {"error": "proxy_unreachable", "detail": str(exc)}


def _proxy_get(path: str, params: Optional[dict[str, Any]] = None) -> tuple[int, dict[str, Any]]:
    return _proxy_request("GET", path, params=params)


def _proxy_post(
    path: str, body: Optional[dict[str, Any]] = None, params: Optional[dict[str, Any]] = None
) -> tuple[int, dict[str, Any]]:
    return _proxy_request("POST", path, params=params, body=body)


# ---------------------------------------------------------------------------
# Vault access — exposes V1/V4/V6/V8 Free features as MCP tools.
# Thin HTTP wrappers over the proxy's /tpk/v1/vault/* endpoints so the
# companion does NOT hold its own VaultIndex instance.
# ---------------------------------------------------------------------------


def _requested_vault_project(args: dict[str, Any]) -> str:
    """Return explicit project scope, or this companion session's env pin.

    The proxy is a separate process and may not share TOKENPAK_PROJECT with the
    MCP server. Forwarding the effective pin is therefore part of the request
    contract, not redundant metadata. An explicit tool argument wins, matching
    the canonical explicit → env resolution order.
    """
    explicit = str(args.get("project", "") or "").strip()
    return explicit or os.environ.get("TOKENPAK_PROJECT", "").strip()


def _handle_vault_search(state: CompanionState, args: dict[str, Any]) -> str:
    """Search the vault via the proxy's /tpk/v1/vault/search endpoint."""
    query = str(args.get("query", "")).strip()
    if not query:
        return json.dumps({"error": "query is required"})
    try:
        limit = int(args.get("limit", 5))
    except (TypeError, ValueError):
        limit = 5
    limit = max(1, min(20, limit))

    params: dict[str, Any] = {"q": query, "limit": limit}
    # Scope the search to one project. Without it a vault holding several
    # projects can return a blend that reads as a single coherent answer.
    project = _requested_vault_project(args)
    if project:
        params["project"] = project
    # Forwarded only when the caller supplies it. The companion process's own
    # cwd is not a trustworthy stand-in — the MCP server may be launched from
    # anywhere, and inferring scope from the wrong directory would produce a
    # confidently mis-scoped answer, which is the failure mode being fixed.
    cwd = str(args.get("cwd", "")).strip()
    if cwd:
        params["cwd"] = cwd

    status, body = _proxy_get("/tpk/v1/vault/search", params)
    if status == 409:
        # Under-specified rather than failed — surface the candidates so the
        # model can ask which project instead of guessing.
        return json.dumps(body, indent=2)
    if status == 0:
        return json.dumps(
            {
                "error": "proxy_unreachable",
                "detail": body.get("detail", "is the tokenpak proxy running? try `tokenpak start`"),
            }
        )
    if status >= 400:
        return json.dumps(body)
    # Pass through the proxy's response shape as-is; it already matches our contract.
    return json.dumps(body, indent=2)


def _handle_vault_retrieve(state: CompanionState, args: dict[str, Any]) -> str:
    """Fetch a vault block via the proxy's /tpk/v1/vault/block/{id} endpoint."""
    block_id = str(args.get("block_id", "")).strip()
    path_hint = str(args.get("path", "")).strip()
    if not block_id and not path_hint:
        return json.dumps({"error": "provide block_id or path"})

    # If only a path hint is given, resolve via search first to get an exact id.
    # The scope MUST ride along: an unscoped resolve picks an arbitrary
    # project's file whenever several projects hold the same relative path
    # (`notes/pr-100.md` in five repos), and then fetches it by id — a
    # cross-project answer produced by a lookup that looked exact. It fails
    # closed today only by accident, because a 409 body carries an empty
    # `results` list that falls through to `block_not_found`.
    project = _requested_vault_project(args)
    cwd = str(args.get("cwd", "")).strip()
    if not block_id and path_hint:
        resolve_params: dict[str, Any] = {"q": path_hint, "limit": 1}
        if project:
            resolve_params["project"] = project
        if cwd:
            resolve_params["cwd"] = cwd
        status, body = _proxy_get("/tpk/v1/vault/search", resolve_params)
        if status == 409:
            # Ambiguous scope on the resolve leg — report it as such rather
            # than as a missing block, which would send the caller looking for
            # the wrong problem.
            return json.dumps(body, indent=2)
        if status == 0:
            return json.dumps({"error": "proxy_unreachable", "detail": body.get("detail", "")})
        results = body.get("results") or []
        if not results:
            return json.dumps({"error": "block_not_found", "path": path_hint})
        block_id = results[0].get("block_id") or ""

    fetch_params: dict[str, Any] = {}
    if project:
        fetch_params["project"] = project
    if cwd:
        fetch_params["cwd"] = cwd
    status, body = _proxy_get(
        f"/tpk/v1/vault/block/{_url_parse.quote(block_id, safe='')}", fetch_params or None
    )
    if status == 0:
        return json.dumps({"error": "proxy_unreachable", "detail": body.get("detail", "")})
    if status >= 400:
        return json.dumps(body)
    return json.dumps(body, indent=2)


# ---------------------------------------------------------------------------
# Tool registry — add new tools here
# ---------------------------------------------------------------------------

TOOLS: list[ToolDef] = [
    ToolDef(
        name="estimate_tokens",
        description="Estimate token count for text or a file. Use before reading large files or including verbose context to decide if it's worth the cost.",
        input_schema={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text to estimate tokens for"},
                "file_path": {
                    "type": "string",
                    "description": "Path to a file (alternative to text)",
                },
            },
        },
        handler=_handle_estimate_tokens,
    ),
    ToolDef(
        name="check_budget",
        description="Check remaining cost budget for this session and today. Call before starting expensive multi-step tasks.",
        input_schema={"type": "object", "properties": {}},
        handler=_handle_check_budget,
    ),
    ToolDef(
        name="load_capsule",
        description="Load a memory capsule from a prior session. Call when resuming work or when the user references past sessions. Omit session_id to list available capsules.",
        input_schema={
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "Session ID to load (omit to list available)",
                },
            },
        },
        handler=_handle_load_capsule,
    ),
    ToolDef(
        name="prune_context",
        description="Compress verbose text (large tool outputs, error logs) to reduce token usage. Keeps the beginning and end, elides the middle.",
        input_schema={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text to prune"},
                "max_tokens": {
                    "type": "integer",
                    "description": "Target token count (default 2000)",
                    "default": 2000,
                },
            },
            "required": ["text"],
        },
        handler=_handle_prune_context,
    ),
    ToolDef(
        name="journal_read",
        description="Read journal entries for this session or a past session. Omit session_id to list recent sessions.",
        input_schema={
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "Session to query (default: current)",
                },
                "entry_type": {
                    "type": "string",
                    "description": "Filter by type: auto, user, milestone, cost",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max entries to return (default 20)",
                    "default": 20,
                },
            },
        },
        handler=_handle_journal_read,
    ),
    ToolDef(
        name="journal_write",
        description="Add a note to the current session journal. Use for important decisions, milestones, or context the user might want later.",
        input_schema={
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "The journal note to save"},
            },
            "required": ["content"],
        },
        handler=_handle_journal_write,
    ),
    ToolDef(
        name="session_info",
        description="Get companion status, session stats, and configuration.",
        input_schema={"type": "object", "properties": {}},
        handler=_handle_session_info,
    ),
    ToolDef(
        name="vault_search",
        description=(
            "Search the indexed vault by BM25 and return top-K matching blocks "
            "with relevance scores. Use when the user references project docs, "
            "code, or knowledge stored in the local vault. The proxy also "
            "auto-injects vault context, but this tool lets you query "
            "explicitly (e.g. narrowing to a specific concept). "
            "If the vault holds several projects, pass `project` to scope the "
            "search. Project scoping requires a configured project registry and "
            "a backend that supports it: when supported, an under-specified "
            "query returns `ambiguous_project_scope` with the candidates instead "
            "of blending projects, and an unsupported `project` request returns "
            "`scoping_unsupported` rather than silently ignoring the scope. "
            "Where scoping is unavailable, results may span projects — check the "
            "`scope` field before treating results as belonging to one project."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query (words or phrase)"},
                "limit": {
                    "type": "integer",
                    "description": "Max results (default 5, max 20)",
                    "default": 5,
                },
                "project": {
                    "type": "string",
                    "description": (
                        "Restrict results to this declared project id. Blocks "
                        "outside it are never scored. Omit to resolve scope "
                        "from the working directory or the query text."
                    ),
                },
                "cwd": {
                    "type": "string",
                    "description": (
                        "Absolute working directory of the task, used to infer "
                        "the project when `project` is absent. Pass it only if "
                        "it really is the directory the work concerns."
                    ),
                },
            },
            "required": ["query"],
        },
        handler=_handle_vault_search,
    ),
    ToolDef(
        name="vault_retrieve",
        description=(
            "Fetch the full content of a specific vault block by block_id "
            "(exact match, from vault_search results) or by path substring "
            "(first match). Returns content + metadata."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "block_id": {"type": "string", "description": "Exact block_id from vault_search"},
                "project": {
                    "type": "string",
                    "description": (
                        "Restrict to this declared project. Required to make a "
                        "`path` lookup unambiguous when several projects hold "
                        "the same relative path; also verified on a block_id "
                        "fetch, which otherwise bypasses scoping entirely."
                    ),
                },
                "cwd": {
                    "type": "string",
                    "description": (
                        "Absolute working directory used to infer the project "
                        "when `project` is absent."
                    ),
                },
                "path": {
                    "type": "string",
                    "description": "Path substring to match (alternative to block_id)",
                },
            },
        },
        handler=_handle_vault_retrieve,
    ),
]
