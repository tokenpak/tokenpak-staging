"""
TokenPak Provider Router

Routes requests to appropriate LLM providers (Anthropic, OpenAI, Google).
Handles provider detection, cost estimation, and URL construction.
"""

import json
from dataclasses import dataclass
from typing import Collection, Dict, Mapping, Optional, cast
from urllib.parse import urlparse, urlsplit, urlunsplit

# Provider base URLs
PROVIDER_URLS = {
    "anthropic": "https://api.anthropic.com",
    "openai": "https://api.openai.com",
    # openai-codex: ChatGPT subscription OAuth.  Clients address the standard
    # /v1/responses surface; TokenPak rewrites it to the ChatGPT backend path.
    "openai-codex": "https://chatgpt.com/backend-api",
    "google": "https://generativelanguage.googleapis.com",
}

# Hosts we intercept for logging/processing.
# Custom providers registered in config.yaml are added at startup by config.py.
INTERCEPT_HOSTS = {
    "api.anthropic.com",
    "api.openai.com",
    "chatgpt.com",
    "generativelanguage.googleapis.com",
}


def _is_codex_oauth_authorization(headers: Dict[str, str]) -> bool:
    """Return true for a supplied OAuth bearer rather than an OpenAI API key.

    The token value is used only for in-memory route classification and is
    never copied into a route result or log record.
    """
    lower_headers = {key.lower(): value for key, value in headers.items()}
    authorization = lower_headers.get("authorization", "").strip()
    if not authorization.lower().startswith("bearer "):
        return False
    credential = authorization[7:].strip()
    return bool(credential) and not credential.lower().startswith("sk-")


def normalize_hostname(value: str) -> str:
    """Return a lower-case hostname from a URL or Host-header value.

    Ports, userinfo, IPv6 brackets, and a terminal DNS dot are normalized by
    the parser.  Invalid or hostname-free values return an empty string.
    """
    candidate = str(value).strip()
    if not candidate:
        return ""
    try:
        parsed = urlsplit(
            candidate if "://" in candidate or candidate.startswith("//") else f"//{candidate}"
        )
        return (parsed.hostname or "").lower().rstrip(".")
    except (TypeError, ValueError):
        return ""


def request_hostname(path: str, headers: Mapping[str, str]) -> str:
    """Resolve the destination hostname from absolute-form path or Host."""
    if "://" in path or path.startswith("//"):
        hostname = normalize_hostname(path)
        if hostname:
            return hostname
    for key, value in headers.items():
        if str(key).lower() == "host":
            return normalize_hostname(str(value))
    return ""


def should_intercept(url: str, hosts: Optional[Collection[str]] = None) -> bool:
    """Return True only for an exact hostname in *hosts* or the live registry."""
    hostname = normalize_hostname(url)
    intercept_hosts = INTERCEPT_HOSTS if hosts is None else hosts
    return bool(hostname) and hostname in {normalize_hostname(host) for host in intercept_hosts}


def _join_upstream_url(base_url: str, request_path: str) -> str:
    """Join a configured API base and reverse-proxy request path once."""
    base = urlsplit(base_url)
    request = urlsplit(request_path)
    base_path = base.path.rstrip("/")
    incoming_path = request.path or "/"

    if not base_path:
        joined_path = incoming_path
    else:
        # Custom endpoints commonly end in /v1 while clients send a path that
        # also begins /v1.  Preserve any earlier gateway prefix but do not
        # duplicate the shared terminal API segment.
        terminal = "/" + base_path.rsplit("/", 1)[-1]
        if incoming_path == terminal or incoming_path.startswith(terminal + "/"):
            joined_path = base_path + incoming_path[len(terminal) :]
        elif incoming_path == base_path or incoming_path.startswith(base_path + "/"):
            joined_path = incoming_path
        else:
            joined_path = f"{base_path}/{incoming_path.lstrip('/')}"

    return urlunsplit(
        (base.scheme, base.netloc, joined_path, request.query or base.query, request.fragment)
    )


# Default costs for unknown models (used as fallback if registry unavailable)
DEFAULT_COSTS = {"input": 3.0, "output": 15.0}


@dataclass
class RouteResult:
    """Result of routing a request."""

    provider: str  # "anthropic", "openai", "openai-codex", "google", "unknown"
    base_url: str
    full_url: str
    should_intercept: bool  # Whether to apply compression/logging
    model: str
    auth_type: str = "apikey"  # "apikey" | "oauth" | "none"
    is_codex: bool = False  # True for Codex subscription models
    skip_cache_keying: bool = False  # True for OAuth (token may expire)


class ProviderRouter:
    """
    Routes requests to appropriate LLM providers.

    Detection priority:
    1. Explicit path patterns (/v1/messages → Anthropic, /v1/chat/completions → OpenAI)
    2. Header presence (x-api-key → Anthropic, Bearer → OpenAI)
    3. Request body model field
    """

    def __init__(
        self,
        custom_urls: Optional[Dict[str, str]] = None,
        custom_hosts: Optional[Dict[str, str]] = None,
    ):
        """
        Initialize router with optional custom provider URLs.

        Args:
            custom_urls: Override default provider URLs (e.g., for proxies)
        """
        self.provider_urls = {**PROVIDER_URLS}
        if custom_urls:
            self.provider_urls.update(custom_urls)
        self.custom_hosts = {
            hostname: provider
            for raw_hostname, provider in (custom_hosts or {}).items()
            if (hostname := normalize_hostname(raw_hostname)) and provider in self.provider_urls
        }

    def route(
        self,
        path: str,
        headers: Dict[str, str],
        body: Optional[bytes] = None,
    ) -> RouteResult:
        """
        Route a request to the appropriate provider.

        Args:
            path: Request path (may be full URL or just path)
            headers: Request headers
            body: Optional request body (for model detection)

        Returns:
            RouteResult with provider info and full URL

        Raises:
            ValueError: If Content-Length header doesn't match actual body size.
        """
        # Validate Content-Length before doing any routing work — only when body
        # is actually provided to this call (body=None means caller omitted it
        # for routing purposes; skip validation in that case).
        lower_headers = {k.lower(): v for k, v in headers.items()}
        content_length_str = lower_headers.get("content-length")
        if content_length_str is not None and body is not None:
            try:
                declared = int(content_length_str)
            except (ValueError, TypeError):
                raise ValueError(f"Invalid Content-Length header value: {content_length_str!r}")
            actual = len(body)
            if declared != actual:
                raise ValueError(
                    f"Content-Length mismatch: header says {declared}, body is {actual} bytes"
                )

        # Check if it's already a full URL
        if path.startswith("http"):
            parsed = urlparse(path)
            hostname = normalize_hostname(path)
            provider = self.custom_hosts.get(hostname) or self._detect_provider_from_host(
                parsed.netloc
            )
            model = self._extract_model(body) if body else "unknown"
            from .oauth import analyze_request as _analyze_oauth

            _oauth_ctx = _analyze_oauth(parsed.path, headers, model)
            return RouteResult(
                provider=provider,
                base_url=f"{parsed.scheme}://{parsed.netloc}",
                full_url=path,
                should_intercept=should_intercept(path),
                model=model,
                auth_type=_oauth_ctx.auth_type,
                is_codex=_oauth_ctx.is_codex,
                skip_cache_keying=_oauth_ctx.skip_cache_keying,
            )

        # Reverse proxy mode - determine provider from headers/path
        hostname = request_hostname(path, headers)
        provider = self.custom_hosts.get(hostname) or self._detect_provider(path, headers, body)
        base_url = self.provider_urls.get(provider, self.provider_urls["anthropic"])
        model = self._extract_model(body) if body else "unknown"
        from .oauth import analyze_request as _analyze_oauth

        _oauth_ctx = _analyze_oauth(path, headers, model)
        upstream_path = path
        if provider == "openai-codex" and path.split("?", 1)[0] == "/v1/responses":
            suffix = "?" + path.split("?", 1)[1] if "?" in path else ""
            upstream_path = "/codex/responses" + suffix
        full_url = (
            _join_upstream_url(base_url, upstream_path)
            if provider.startswith("custom-")
            else base_url + upstream_path
        )
        return RouteResult(
            provider=provider,
            base_url=base_url,
            full_url=full_url,
            should_intercept=True,  # Reverse proxy always intercepts
            model=model,
            auth_type=_oauth_ctx.auth_type,
            is_codex=_oauth_ctx.is_codex,
            skip_cache_keying=_oauth_ctx.skip_cache_keying,
        )

    def _detect_provider_from_host(self, host: str) -> str:
        """Detect provider from hostname."""
        host_lower = normalize_hostname(host)
        if host_lower == "anthropic.com" or host_lower.endswith(".anthropic.com"):
            return "anthropic"
        elif host_lower == "chatgpt.com" or host_lower.endswith(".chatgpt.com"):
            return "openai-codex"
        elif host_lower == "openai.com" or host_lower.endswith(".openai.com"):
            return "openai"
        elif (
            host_lower == "googleapis.com"
            or host_lower.endswith(".googleapis.com")
            or host_lower == "google.com"
            or host_lower.endswith(".google.com")
        ):
            return "google"
        return "unknown"

    def _detect_provider(
        self,
        path: str,
        headers: Dict[str, str],
        body: Optional[bytes] = None,
    ) -> str:
        """Detect provider from path, headers, and body.

        Detection priority:
        1. Path patterns (/v1/messages → anthropic, /v1/responses → openai-codex)
        2. Anthropic-specific headers (x-api-key, anthropic-version)
        3. Body model name (claude → anthropic, codex → openai-codex, gpt → openai)
        4. Bearer token presence (non-Google Bearer → openai)
        5. Default: anthropic
        """
        # Path-based detection (highest priority)
        if "/v1/messages" in path:
            return "anthropic"
        if "/codex/responses" in path or "/v1/codex/responses" in path:
            # ChatGPT Codex backend path (OpenClaw's tokenpak-openai-codex
            # provider posts here). Proxy injects the JWT from
            # ~/.codex/auth.json and rewrites to chatgpt.com/backend-api.
            return "openai-codex"
        if "/v1/responses" in path:
            # The same Responses path is used by both OpenAI API-key clients
            # and Codex subscription clients.  Preserve API-key traffic on
            # api.openai.com; only an OAuth bearer selects the ChatGPT backend.
            return "openai-codex" if _is_codex_oauth_authorization(headers) else "openai"
        if "/chat/completions" in path:
            return "openai"
        if "/models/" in path and "generateContent" in path:
            return "google"

        # Anthropic-specific header detection
        lower_headers = {k.lower(): v for k, v in headers.items()}
        if lower_headers.get("x-api-key") or lower_headers.get("anthropic-version"):
            return "anthropic"

        # Body-based detection (model name patterns)
        if body:
            model = self._extract_model(body)
            if model.startswith("claude"):
                return "anthropic"
            if "codex" in model.lower():
                return "openai-codex"
            if model.startswith("gpt") or model.startswith("o1") or model.startswith("o3"):
                return "openai"
            if model.startswith("gemini"):
                return "google"

        # Header-based detection (lower priority than path/body)
        auth = lower_headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            if "google" in path.lower():
                return "google"
            return "openai"

        # Default to Anthropic (most common reverse-proxy use case)
        return "anthropic"

    def _extract_model(self, body: bytes) -> str:
        """Extract model name from request body."""
        try:
            data = json.loads(body)
            return cast(str, data.get("model", "unknown"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return "unknown"


def estimate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
) -> float:
    """
    Estimate cost for a request in dollars.

    Args:
        model: Model name (e.g., "claude-sonnet-4-5")
        input_tokens: Number of input tokens
        output_tokens: Number of output tokens
        cache_read_tokens: Tokens read from cache (90% discount)
        cache_creation_tokens: Tokens written to cache (25% premium)

    Returns:
        Estimated cost in dollars
    """
    # Get costs from dynamic registry
    from tokenpak.models import get_model_costs

    costs = get_model_costs(model) if model else DEFAULT_COSTS

    # Calculate regular input (excluding cache tokens)
    regular_input = max(0, input_tokens - cache_read_tokens - cache_creation_tokens)

    # Apply costs
    input_cost = regular_input * costs["input"]
    cache_read_cost = cache_read_tokens * costs["input"] * 0.1  # 90% discount
    cache_creation_cost = cache_creation_tokens * costs["input"] * 1.25  # 25% premium
    output_cost = output_tokens * costs["output"]

    total = (input_cost + cache_read_cost + cache_creation_cost + output_cost) / 1_000_000
    return total


def get_model_tier(model: str) -> str:
    """
    Get the tier (pricing category) for a model.

    Returns: "premium", "standard", "economy", or "unknown"
    """
    model_lower = model.lower()

    if (
        any(x in model_lower for x in ["opus", "gpt-4-turbo", "gpt-4"])
        and "mini" not in model_lower
    ):
        return "premium"
    elif (
        any(x in model_lower for x in ["sonnet", "gpt-4o", "gemini-1.5-pro"])
        and "mini" not in model_lower
    ):
        return "standard"
    elif any(x in model_lower for x in ["haiku", "mini", "gpt-3.5", "gemini-pro"]):
        return "economy"

    return "unknown"


# ---------------------------------------------------------------------------
# Vault retrieval helpers — re-exported here for proxy-layer consumers
# ---------------------------------------------------------------------------
# These functions provide cache-stable BM25 retrieval injection used by the
# proxy to keep prompt structures byte-identical across repeated requests.

from tokenpak.vault.search import (  # noqa: E402
    DEFAULT_MAX_TOKENS,
    RETRIEVED_CONTEXT_HEADER,
    inject_retrieved_context,
    measure_injection_consistency,
    sort_retrieval_results,
)

__all__ = [
    "ProviderRouter",
    "RouteResult",
    "estimate_cost",
    "get_model_tier",
    "sort_retrieval_results",
    "inject_retrieved_context",
    "measure_injection_consistency",
    "RETRIEVED_CONTEXT_HEADER",
    "DEFAULT_MAX_TOKENS",
]
