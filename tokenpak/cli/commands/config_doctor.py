# SPDX-License-Identifier: Apache-2.0
"""config doctor — read-only configuration diagnostics.

Focused diagnostic for the *configuration subsystem*: where config is read
from, in what precedence, which env vars are set, whether the proxy-attach
is wired, and whether user/system file boundaries are intact. Complements
(does not duplicate) the broad ``tokenpak doctor``.

Design: vault ``01_PROJECTS/tokenpak/design/
centralized-env-packet-b-config-doctor-init-loadorder-design-2026-06-06.md``
(§1). Load-order spec: ``docs/configuration/env-load-order.md``.

Hard invariants (design §1.1, §1.4):

* **Read-only.** This module never creates, writes, moves, chmods, or
  deletes any file. It reads config files, stats directories, and reads
  ``os.environ`` — nothing else. In particular it must NOT call
  ``config_loader.load_config()`` (which auto-migrates config.json →
  config.yaml, i.e. writes); files are parsed directly instead.
* **Mask-always.** No secret *value* is ever printed — for any var, output
  shows presence + provenance only (``set (env)`` / ``not set``). There is
  no ``--show-secrets`` flag.

Checks (design §1.2): D1 home resolution, D2 config-file presence + parse,
D3 effective precedence chain, D4 env vars vs schema, D5 ANTHROPIC_BASE_URL
attach state, D6 .env hygiene, D7 user/system boundary drift, D8 schema
coverage.

Exit codes (design §1.5 / Std 03 §3): 0 = ok/info/warn (advisory), 4 = any
``fail`` check (config error), 2 = usage (argparse), 1 = unexpected error.
"""

from __future__ import annotations

import json
import os
import re
import stat as stat_mod
import subprocess
from pathlib import Path

from tokenpak import _paths

# ---------------------------------------------------------------------------
# Env-var name manifest (schema-as-data, not a gate)
# ---------------------------------------------------------------------------
# Build-time generated manifest derived from the canonical Packet-A schema
# (vault 01_PROJECTS/tokenpak/config/env-schema.md) + the schema's own
# dynamic-discovery contract. Regenerate with:
#
#   grep -rhoE "TOKENPAK_[A-Z0-9_]+" tokenpak/ --include="*.py" | sort -u
#
# Per `feedback_always_dynamic` this is documentation-as-data, never a gate:
# an unknown TOKENPAK_* name yields a *warn* (possible typo), never a fail,
# and resolution elsewhere stays graceful (design §1.2 D4, spec §3.4).

KNOWN_TOKENPAK_VARS: frozenset[str] = frozenset({
    "TOKENPAK_ACTIVE_PROFILE", "TOKENPAK_ADMIN_BOOTSTRAP", "TOKENPAK_AGENT",
    "TOKENPAK_ALERT_CHANNEL", "TOKENPAK_ALERT_CHAT_ID", "TOKENPAK_ALERT_EMAIL_TO",
    "TOKENPAK_ALERT_HOSTNAME", "TOKENPAK_ALERT_WEBHOOK_HEADERS", "TOKENPAK_ALERT_WEBHOOK_URL",
    "TOKENPAK_ALLOWED_KEYS", "TOKENPAK_ALLOW_MODEL_DOWNLOAD", "TOKENPAK_API_KEY",
    "TOKENPAK_ATTRIBUTION_V2", "TOKENPAK_AUTH_ALERT_COOLDOWN", "TOKENPAK_AUTH_FAILURE_THRESHOLD",
    "TOKENPAK_BACKOFF_BASE", "TOKENPAK_BACKOFF_CAP", "TOKENPAK_BASE_URL",
    "TOKENPAK_BIND_ADDRESS", "TOKENPAK_BM25_MIN_SCORE", "TOKENPAK_BM25_WEIGHT",
    "TOKENPAK_BROKER_MIN_SAMPLES", "TOKENPAK_BUDGET_ALERT_PCT", "TOKENPAK_BUDGET_CONTROLLER",
    "TOKENPAK_BUDGET_DAILY_LIMIT_USD", "TOKENPAK_BUDGET_TOTAL", "TOKENPAK_CACHEABLE_INJECTION",
    "TOKENPAK_CACHE_ALERT_SLACK_CHANNEL", "TOKENPAK_CACHE_ALERT_THRESHOLD",
    "TOKENPAK_CACHE_ALERT_WEBHOOK_ENABLED", "TOKENPAK_CACHE_ALERT_WEBHOOK_URL",
    "TOKENPAK_CACHE_DEFAULT_MODE", "TOKENPAK_CACHE_ENABLED", "TOKENPAK_CACHE_FALLBACK",
    "TOKENPAK_CACHE_INVALIDATION_ALERT_THRESHOLD_USD", "TOKENPAK_CACHE_MISS_DEBUG",
    "TOKENPAK_CACHE_REGISTRY", "TOKENPAK_CACHE_TTL", "TOKENPAK_CAPSULES",
    "TOKENPAK_CAPSULE_BUILDER", "TOKENPAK_CAPSULE_BUILDER_ENABLED",
    "TOKENPAK_CAPSULE_HOT_WINDOW", "TOKENPAK_CAPSULE_MIN_CHARS", "TOKENPAK_CB_ENABLED",
    "TOKENPAK_CB_FAILURE_THRESHOLD", "TOKENPAK_CB_MIN_FAILURE_RATIO",
    "TOKENPAK_CB_RECOVERY_TIMEOUT", "TOKENPAK_CB_WINDOW_SECONDS",
    "TOKENPAK_CC_INJECT_MAX_CHARS", "TOKENPAK_CC_INJECT_MIN_QUERY", "TOKENPAK_CFG",
    "TOKENPAK_CHAT_FOOTER", "TOKENPAK_CHATGPT_BASE_URL", "TOKENPAK_CHATGPT_PROFILE_TOML",
    "TOKENPAK_CLAUDE_BIN", "TOKENPAK_CLIENT", "TOKENPAK_COMPACT",
    "TOKENPAK_COMPACT_CACHE_SIZE", "TOKENPAK_COMPACT_MAX_CHARS", "TOKENPAK_COMPACT_MAX_TOKENS",
    "TOKENPAK_COMPACT_THRESHOLD_TOKENS", "TOKENPAK_COMPANION_BARE", "TOKENPAK_COMPANION_BUDGET",
    "TOKENPAK_COMPANION_ENABLED", "TOKENPAK_COMPANION_EXTRA_DIRS", "TOKENPAK_COMPANION_HOOKS",
    "TOKENPAK_COMPANION_JOURNAL_DIR", "TOKENPAK_COMPANION_MCP", "TOKENPAK_COMPANION_MEMORY_DIRS",
    "TOKENPAK_COMPANION_PROFILE", "TOKENPAK_COMPANION_PRUNE_THRESHOLD",
    "TOKENPAK_COMPANION_SHOW_COST", "TOKENPAK_COMPLIANCE_PROVIDER", "TOKENPAK_COMPRESSION",
    "TOKENPAK_COMPRESSION_DICT", "TOKENPAK_CONCURRENCY", "TOKENPAK_CONFIG",
    "TOKENPAK_CONSUMPTION_MODE", "TOKENPAK_CORS_ORIGINS", "TOKENPAK_COST_TRACKING",
    "TOKENPAK_CREDS_ROUTER_ENABLED", "TOKENPAK_DASHBOARD_AUTH", "TOKENPAK_DASHBOARD_URL",
    "TOKENPAK_DB", "TOKENPAK_DB_PATH", "TOKENPAK_DEBUG", "TOKENPAK_DEBUG_CAPTURE",
    "TOKENPAK_DEBUG_CAPTURE_KEY", "TOKENPAK_DISABLE_DOCS", "TOKENPAK_DISCOVERY_INTERVAL",
    "TOKENPAK_DLP_ENABLED", "TOKENPAK_DLP_MODE", "TOKENPAK_EMBEDDING_CACHE_MAX_MB",
    "TOKENPAK_EMBEDDING_CACHE_TTL_DAYS", "TOKENPAK_EMBEDDING_CONTENT_ROUTING",
    "TOKENPAK_EMBEDDING_LENGTH_THRESHOLD", "TOKENPAK_EMBEDDING_ROUTING_STRATEGY",
    "TOKENPAK_ENABLE_PLUGINS", "TOKENPAK_ENTRIES_DIR", "TOKENPAK_ENV",
    "TOKENPAK_ERROR_NORMALIZER", "TOKENPAK_EVENT_DATA", "TOKENPAK_EVENT_TYPE",
    "TOKENPAK_FAILURE_MEMORY", "TOKENPAK_FIDELITY_TIERS", "TOKENPAK_HEADER_ALLOWLIST",
    "TOKENPAK_HOME", "TOKENPAK_HOOK", "TOKENPAK_HOOK_EVENTS", "TOKENPAK_HOOK_MARKER",
    "TOKENPAK_HTTP100_KEEPALIVE", "TOKENPAK_HTTP2", "TOKENPAK_HTTPX_POOL_SIZE",
    "TOKENPAK_HTTPX_TIMEOUT", "TOKENPAK_INCIDENT_LOG", "TOKENPAK_INDEX_CLAUDE_TRANSCRIPTS",
    "TOKENPAK_INJECT_BUDGET", "TOKENPAK_INJECT_MIN_PROMPT", "TOKENPAK_INJECT_MIN_SCORE",
    "TOKENPAK_INJECT_SCRIPT_DIR", "TOKENPAK_INJECT_SKIP_MODELS", "TOKENPAK_INJECT_TOP_K",
    "TOKENPAK_INPUT_RATE", "TOKENPAK_INTELLIGENCE_URL", "TOKENPAK_JOB_NAME",
    "TOKENPAK_KEY_COOLDOWN_401", "TOKENPAK_KEY_COOLDOWN_429", "TOKENPAK_KEY_ROTATION",
    "TOKENPAK_LICENSE_DEV_SHIM", "TOKENPAK_LICENSE_FILE", "TOKENPAK_LICENSE_SERVER",
    "TOKENPAK_LOCAL_FIRST_ROUTING", "TOKENPAK_LOG_DESTINATION", "TOKENPAK_LOG_ENABLED",
    "TOKENPAK_LOG_LEVEL", "TOKENPAK_LOG_REQUEST_BODY", "TOKENPAK_LOG_RESPONSE_BODY",
    "TOKENPAK_LOG_RETENTION_DAYS", "TOKENPAK_MACHINE", "TOKENPAK_MANAGED",
    "TOKENPAK_MAX_REQUEST_SIZE", "TOKENPAK_MAX_RETRIES", "TOKENPAK_MEMORY_BUDGET_MAX",
    "TOKENPAK_MEMORY_CEILING_MB", "TOKENPAK_MEMORY_CHECK_SECS", "TOKENPAK_MEMORY_GUARD",
    "TOKENPAK_MEMORY_PROXY_SHARE", "TOKENPAK_MEMORY_SYS_LOW_MB", "TOKENPAK_MEMORY_TARGET_MB",
    "TOKENPAK_METRICS_ENABLED", "TOKENPAK_METRICS_INGEST_URL", "TOKENPAK_METRICS_URL",
    "TOKENPAK_MODE", "TOKENPAK_MODEL_DISCOVERY", "TOKENPAK_MONITOR_DB",
    "TOKENPAK_MUTATION_AUDIT_TTL_DAYS", "TOKENPAK_NONINTERACTIVE", "TOKENPAK_NO_COLOR",
    "TOKENPAK_NO_THREADS", "TOKENPAK_NO_UPDATE_CHECK", "TOKENPAK_OLLAMA_ENABLED",
    "TOKENPAK_OLLAMA_TIMEOUT", "TOKENPAK_OLLAMA_UPSTREAM", "TOKENPAK_OLLAMA_URL",
    "TOKENPAK_OPENAI_PROXY_URL", "TOKENPAK_OPENCLAW_FALLBACK", "TOKENPAK_OPTIMIZATION_PIPELINE",
    "TOKENPAK_OTEL_ENDPOINT", "TOKENPAK_OTLP_ENDPOINT", "TOKENPAK_PLATFORM_ADAPTERS",
    "TOKENPAK_PLUGINS", "TOKENPAK_POOL_KEEPALIVE_EXPIRY", "TOKENPAK_POOL_MAX_CONNECTIONS",
    "TOKENPAK_POOL_MAX_KEEPALIVE", "TOKENPAK_PORT", "TOKENPAK_PRECONDITION_GATES",
    "TOKENPAK_PREFIX_REGISTRY", "TOKENPAK_PROFILE", "TOKENPAK_PROFILE_OVERRIDE",
    "TOKENPAK_PROGRESSIVE_DISCLOSURE", "TOKENPAK_PROXY_AUTH_TOKEN",
    "TOKENPAK_PROXY_CAPTURE_INTAKE", "TOKENPAK_PROXY_KEY", "TOKENPAK_PROXY_URL",
    "TOKENPAK_PRUNE_ANTIPATTERNS", "TOKENPAK_QUERY_EXPANSION_ENABLED",
    "TOKENPAK_QUERY_REWRITER", "TOKENPAK_RATE_LIMIT_COOLDOWN_SEC", "TOKENPAK_RATE_LIMIT_RPM",
    "TOKENPAK_RATE_LIMIT_THRESHOLD", "TOKENPAK_RATE_LIMIT_WINDOW_SEC", "TOKENPAK_RBAC_DB_PATH",
    "TOKENPAK_RECALL_DB", "TOKENPAK_REMOTE_HOST", "TOKENPAK_REQUEST_LOGGER",
    "TOKENPAK_REQUEST_VALIDATION", "TOKENPAK_RETRIEVAL_BACKEND", "TOKENPAK_RETRIEVAL_MODE",
    "TOKENPAK_RETRIEVAL_PRUNING", "TOKENPAK_RETRIEVAL_TOP_K", "TOKENPAK_RETRIEVAL_WATCHDOG",
    "TOKENPAK_ROUTER_ENABLED", "TOKENPAK_ROUTE_COMPRESSION_STAGE", "TOKENPAK_RRF_K",
    "TOKENPAK_SALIENCE_ROUTER", "TOKENPAK_SDK", "TOKENPAK_SEMANTIC_BACKEND",
    "TOKENPAK_SEMANTIC_CACHE", "TOKENPAK_SEMANTIC_CACHE_STAGE", "TOKENPAK_SERVER",
    "TOKENPAK_SESSION_CAPSULES", "TOKENPAK_SESSION_CLIENTS_MAX",
    "TOKENPAK_SESSION_CLIENT_IDLE_SECS", "TOKENPAK_SHADOW_BATCH_SIZE",
    "TOKENPAK_SHADOW_ENABLED", "TOKENPAK_SHADOW_LOG", "TOKENPAK_SHADOW_LOG_METRICS",
    "TOKENPAK_SHADOW_LOG_REQUESTS", "TOKENPAK_SHADOW_LOG_RESPONSES", "TOKENPAK_SHADOW_MODE",
    "TOKENPAK_SHADOW_READER", "TOKENPAK_SHELL", "TOKENPAK_SHUTDOWN_TIMEOUT",
    "TOKENPAK_SKELETON_ENABLED", "TOKENPAK_SKIP_GATE", "TOKENPAK_SLACK_WEBHOOK",
    "TOKENPAK_SMTP_HOST", "TOKENPAK_SMTP_PASS", "TOKENPAK_SMTP_PORT", "TOKENPAK_SMTP_USER",
    "TOKENPAK_SNAPSHOT_GEN", "TOKENPAK_SSRM_ENABLED", "TOKENPAK_STABILITY_SCORER",
    "TOKENPAK_STABLE_CACHE_CONTROL_AUTO", "TOKENPAK_STATS_FOOTER", "TOKENPAK_STRICT_MODE",
    "TOKENPAK_SUBCMDS", "TOKENPAK_SWAP_ALERT_COOLDOWN_S", "TOKENPAK_SWAP_ALERT_MB",
    "TOKENPAK_SWAP_ALERT_THRESHOLD_MB", "TOKENPAK_SWAP_SELF_HEAL_COOLDOWN_S",
    "TOKENPAK_SWAP_SELF_HEAL_SCRIPT", "TOKENPAK_SWAP_WARN_MB", "TOKENPAK_TEAM_VAULT",
    "TOKENPAK_TELEGRAM_BOT_TOKEN", "TOKENPAK_TELEGRAM_CHAT_ID", "TOKENPAK_TELEMETRY_CONFIG",
    "TOKENPAK_TELEMETRY_DB", "TOKENPAK_TELEMETRY_DIR", "TOKENPAK_TELEMETRY_MODE",
    "TOKENPAK_TELEMETRY_SCRIPT", "TOKENPAK_TERM_RESOLVER_ENABLED",
    "TOKENPAK_TERM_RESOLVER_MAX_BYTES", "TOKENPAK_TERM_RESOLVER_TOP_K",
    "TOKENPAK_TOOL_SCHEMA_STABILITY", "TOKENPAK_TRACE", "TOKENPAK_UPSTREAM_ACQUIRE_TIMEOUT",
    "TOKENPAK_UPSTREAM_ANTHROPIC_MESSAGES", "TOKENPAK_UPSTREAM_CONCURRENCY",
    "TOKENPAK_UPSTREAM_PASSTHROUGH", "TOKENPAK_UPSTREAM_RETRIES", "TOKENPAK_UPSTREAM_TIMEOUT",
    "TOKENPAK_URL", "TOKENPAK_USAGE_SPOOL_DIR", "TOKENPAK_USER_ID", "TOKENPAK_USE_MONOLITH",
    "TOKENPAK_USE_SQLITE_BLOCKS", "TOKENPAK_VALIDATION_GATE",
    "TOKENPAK_VALIDATION_GATE_BUDGET_CAP", "TOKENPAK_VALIDATION_GATE_SOFT",
    "TOKENPAK_VALIDATION_STRICT", "TOKENPAK_VAULT_AUTO_REINDEX", "TOKENPAK_VAULT_CACHE_PRELOAD",
    "TOKENPAK_VAULT_CONFIG", "TOKENPAK_VAULT_INDEX", "TOKENPAK_VAULT_INDEX_PATH",
    "TOKENPAK_VAULT_INDEX_RELOAD_INTERVAL", "TOKENPAK_VAULT_INJECT_ENABLED",
    "TOKENPAK_VAULT_MEMORY_MAX", "TOKENPAK_VAULT_ROOT", "TOKENPAK_VECTOR_INDEX_PATH",
    "TOKENPAK_VECTOR_MODEL", "TOKENPAK_VECTOR_WEIGHT", "TOKENPAK_WEBHOOK_URL",
    "TOKENPAK_WORKFLOW_TRACKING", "TOKENPAK_WORKSPACE", "TOKENPAK_WS_MAX_CONNECTIONS",
    "TOKENPAK_WS_PORT",
})

# Variable *families* accepted by prefix (schema groups these by family rather
# than enumerating each member — pools, spend-guard, embeddings, slot vars).
KNOWN_TOKENPAK_PREFIXES: tuple[str, ...] = (
    "TOKENPAK_SPEND_GUARD_",
    "TOKENPAK_SSRM_",
    "TOKENPAK_COMPANION_",
    "TOKENPAK_EMBEDDING_",
    "TOKENPAK_UPSTREAM_",
)

# External provider / integration keys recognized by name (schema sections
# 2-3). Slot pattern: ANTHROPIC_API_KEY_2..N (no fixed cap enumerated).
KNOWN_PROVIDER_VARS: frozenset[str] = frozenset({
    "ANTHROPIC_API_KEY", "ANTHROPIC_OAUTH_TOKEN", "ANTHROPIC_OAUTH_TOKEN2",
    "ANTHROPIC_BASE_URL", "OPENAI_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY",
    "GITHUB_TOKEN", "NOTION_API_TOKEN", "TELEGRAM_BOT_TOKEN", "CODEX_HOME",
})
_PROVIDER_SLOT_RE = re.compile(r"^ANTHROPIC_API_KEY_\d+$")
_OPENCLAW_PREFIX = "OPENCLAW_"  # legacy family, deprecating v1.7→v1.9

# Secret-class heuristics (schema "Secret class" column, by name shape).
# Used ONLY for presence annotations and the D7 committed-surface scan —
# doctor never prints a value for ANY var, secret-class or not.
_HIGH_SECRET_RE = re.compile(
    r"(API_KEY|OAUTH_TOKEN|BOT_TOKEN|AUTH_TOKEN|PROXY_KEY|ALLOWED_KEYS"
    r"|CAPTURE_KEY|SMTP_PASS|SLACK_WEBHOOK|GITHUB_TOKEN|NOTION_API_TOKEN)"
)
_MEDIUM_SECRET_RE = re.compile(
    r"(BASE_URL|WEBHOOK_URL|OTEL_ENDPOINT|OTLP_ENDPOINT|CHAT_ID|EMAIL_TO"
    r"|CORS_ORIGINS|LICENSE_FILE|RBAC_DB_PATH|SMTP_HOST|SMTP_USER)"
)

# Real-credential *shapes* for the D7 committed-surface scan (Std 36 §1.4).
# Matches fire on shape (so clearly-marked fixtures like sk-ant-EXAMPLE...
# still trip the check); the matched text is NEVER echoed in output.
_SECRET_SHAPE_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    ("anthropic-key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}")),
    ("openai-key", re.compile(r"sk-[A-Za-z0-9]{24,}")),
    ("github-pat", re.compile(r"(ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")),
    ("slack-token", re.compile(r"xox[abprs]-[A-Za-z0-9\-]{10,}")),
    ("google-key", re.compile(r"AIza[0-9A-Za-z_\-]{20,}")),
    ("telegram-token", re.compile(r"\b\d{8,10}:[A-Za-z0-9_\-]{30,}\b")),
)

# Curated load-bearing keys for the D3 precedence report:
# (config.yaml dot-path, env var, built-in default) — mirrors config_loader.
_CURATED_KEYS: tuple[tuple[str, str, object], ...] = (
    ("port", "TOKENPAK_PORT", 8766),
    ("mode", "TOKENPAK_MODE", "hybrid"),
    ("db", "TOKENPAK_DB", None),
    ("compression.enabled", "TOKENPAK_COMPACT", True),
    ("vault.index_path", "TOKENPAK_VAULT_INDEX", "~/vault/.tokenpak"),
    ("rate_limit_rpm", "TOKENPAK_RATE_LIMIT_RPM", 60),
)

_LOAD_ORDER_SPEC = "docs/configuration/env-load-order.md"


class _Colors:
    """ANSI color codes + status glyphs (state-only color, Std 03 §4.1)."""

    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    CYAN = "\033[96m"
    RESET = "\033[0m"

    @staticmethod
    def ok(text: str) -> str:
        return f"{_Colors.GREEN}✅{_Colors.RESET}  {text}"

    @staticmethod
    def warn(text: str) -> str:
        return f"{_Colors.YELLOW}⚠️{_Colors.RESET}   {text}"

    @staticmethod
    def fail(text: str) -> str:
        return f"{_Colors.RED}❌{_Colors.RESET}  {text}"

    @staticmethod
    def info(text: str) -> str:
        return f"{_Colors.CYAN}ℹ️{_Colors.RESET}   {text}"


def secret_class(name: str) -> str:
    """Classify an env var name as high / medium / low (schema heuristic)."""
    if _HIGH_SECRET_RE.search(name) or _PROVIDER_SLOT_RE.match(name):
        return "high"
    if _MEDIUM_SECRET_RE.search(name):
        return "medium"
    return "low"


def is_known_var(name: str) -> bool:
    """True when *name* appears in the schema manifest (exact, family, slot)."""
    if name in KNOWN_TOKENPAK_VARS or name in KNOWN_PROVIDER_VARS:
        return True
    if _PROVIDER_SLOT_RE.match(name):
        return True
    if name.startswith(_OPENCLAW_PREFIX):
        return True
    return any(name.startswith(p) for p in KNOWN_TOKENPAK_PREFIXES)


def _home_rule() -> str:
    """Which _paths resolution rule fired: env | canonical | legacy."""
    if os.environ.get(_paths.ENV_VAR, "").strip():
        return "env"
    if _paths.is_legacy_active():
        return "legacy"
    return "canonical"


def _parse_env_names(path: Path) -> list[str]:
    """Read variable *names* from a dotenv-style file. Values are discarded
    immediately and never stored or returned (mask-always, design §1.4)."""
    names: list[str] = []
    try:
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name = line.split("=", 1)[0].strip()
            if name.startswith("export "):
                name = name[len("export "):].strip()
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                names.append(name)
    except OSError:
        pass
    return names


def _git_tracked_candidates(cwd: Path) -> list[Path]:
    """Tracked env/config-shaped files under *cwd* (bounded, read-only).

    Returns [] when git is unavailable or cwd is not a work tree. Candidate
    filter: dotenv-shaped names plus top-level config files — the committed
    surfaces the schema's secret-class guidance covers.
    """
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=str(cwd), capture_output=True, timeout=10, check=True,
        ).stdout.decode("utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError):
        return []
    candidates: list[Path] = []
    for rel in out.split("\0"):
        if not rel:
            continue
        base = Path(rel).name
        if ".env" in base or base in ("config.yaml", "config.json", "tokenpak.yaml"):
            p = cwd / rel
            try:
                if p.is_file() and p.stat().st_size <= 262144:
                    candidates.append(p)
            except OSError:
                continue
        if len(candidates) >= 500:
            break
    return candidates


def _gitignore_covers_env(cwd: Path) -> bool | None:
    """Best-effort: does ./.gitignore cover ``.env``? None = no .gitignore."""
    gi = cwd / ".gitignore"
    if not gi.is_file():
        return None
    try:
        lines = [ln.strip() for ln in gi.read_text(encoding="utf-8").splitlines()]
    except OSError:
        return None
    return any(ln in (".env", "/.env", ".env*", "*.env", ".env.*") for ln in lines)


def run_config_doctor(
    json_output: bool = False,
    quiet: bool = False,
    verbose: bool = False,
) -> int:
    """Run the read-only config diagnostics. Returns the process exit code.

    0 = all ok/info, or advisory warn only; 4 = at least one ``fail``
    (config error, Std 03 §3 dedicated config-error code).
    """
    checks: list[dict] = []
    counts = {"ok": 0, "warn": 0, "fail": 0, "info": 0}

    def record(check_id: str, check: str, status: str, message: str, detail: str = "") -> None:
        checks.append(
            {"id": check_id, "check": check, "status": status,
             "message": message, "detail": detail}
        )
        counts[status] += 1

    home = _paths.home()
    rule = _home_rule()

    # === D1: home resolution =================================================
    if rule == "legacy":
        record(
            "D1", "home_resolution", "warn",
            f"home resolved (legacy)  {home}",
            detail=(
                "Resolution fell back to legacy ~/.tokenpak/ — canonical "
                "~/.tpk/ is absent. Run `tokenpak config migrate` to move "
                "to ~/.tpk/ (Std 33 §8)."
            ),
        )
    else:
        record(
            "D1", "home_resolution", "ok",
            f"home resolved ({rule})  {home}",
            detail=f"rule={rule} via tokenpak._paths.home()",
        )

    # === D2: config file presence + parse ====================================
    # Parsed directly (NOT via config_loader.load_config(), which would
    # auto-migrate config.json -> config.yaml — a write).
    yaml_path = home / "config.yaml"
    json_path = home / "config.json"
    migrated_remnant = home / "config.json.migrated"
    parse_failed = False
    d2_details: list[str] = []
    if yaml_path.is_file():
        try:
            import yaml as _yaml

            _yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
            d2_details.append(f"config.yaml: present, parses ({yaml_path})")
        except Exception as exc:
            parse_failed = True
            d2_details.append(f"config.yaml: UNPARSEABLE ({type(exc).__name__})")
    else:
        d2_details.append("config.yaml: absent (defaults in effect)")
    if json_path.is_file():
        try:
            json.loads(json_path.read_text(encoding="utf-8"))
            d2_details.append(f"config.json toggles: present, parses ({json_path})")
        except Exception as exc:
            parse_failed = True
            d2_details.append(f"config.json: UNPARSEABLE ({type(exc).__name__})")
    if migrated_remnant.is_file():
        d2_details.append(f"config.json.migrated remnant present ({migrated_remnant})")
    if parse_failed:
        record("D2", "config_files", "fail",
               "config file unparseable", detail="; ".join(d2_details))
    elif yaml_path.is_file() or json_path.is_file():
        record("D2", "config_files", "ok",
               "config files present + parse", detail="; ".join(d2_details))
    else:
        record("D2", "config_files", "info",
               "no config files (built-in defaults in effect)",
               detail="; ".join(d2_details))

    # === D3: effective precedence chain ======================================
    # Reports the resolved load order (spec §3.2) and, per curated key, which
    # layer supplies the effective value in the LIVE loader (env -> user
    # config -> default). The .env layers are spec-only (HELD wiring); when a
    # key appears there it is reported as a drift/fallback observation, not
    # as the effective source.
    yaml_cfg: dict = {}
    if yaml_path.is_file() and not parse_failed:
        try:
            import yaml as _yaml

            yaml_cfg = _yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
        except Exception:
            yaml_cfg = {}

    cwd = Path.cwd()
    project_env_names = set(_parse_env_names(cwd / ".env")) if (cwd / ".env").is_file() else set()
    home_env = home / ".env"
    home_env_names = set(_parse_env_names(home_env)) if home_env.is_file() else set()

    def _in_yaml(dot_path: str) -> bool:
        node: object = yaml_cfg
        for part in dot_path.split("."):
            if not isinstance(node, dict) or part not in node:
                return False
            node = node[part]
        return node is not None

    chain = (
        "precedence (highest→lowest): CLI flag > process env > ./.env* > "
        f"<tpk-home>/.env* > project config > user config > defaults "
        f"(*spec layers — not yet wired; see {_LOAD_ORDER_SPEC})"
    )
    record("D3", "precedence_chain", "ok", "load order resolved", detail=chain)
    for dot_path, env_var, default in _CURATED_KEYS:
        if env_var in os.environ:
            layer = "env"
        elif _in_yaml(dot_path):
            layer = "user config"
        else:
            layer = "default"
        notes = []
        if env_var in project_env_names:
            notes.append("also named in ./.env (spec layer 3 — not consulted by live loader)")
        if env_var in home_env_names:
            notes.append("also named in <tpk-home>/.env (spec layer 4 — not consulted)")
        suffix = f" [{'; '.join(notes)}]" if notes else ""
        shown_default = "(unset)" if default is None else default
        record(
            "D3", f"precedence:{dot_path}", "info",
            f"{env_var}  ←  {layer}{suffix}",
            detail=f"key={dot_path} env_var={env_var} default={shown_default}",
        )

    # === D4: env vars set (validated by NAME against the schema manifest) ====
    set_vars = sorted(
        n for n in os.environ
        if n.startswith(("TOKENPAK_", _OPENCLAW_PREFIX))
        or n in KNOWN_PROVIDER_VARS
        or _PROVIDER_SLOT_RE.match(n)
    )
    unknown = [n for n in set_vars if n.startswith("TOKENPAK_") and not is_known_var(n)]
    for name in set_vars:
        cls = secret_class(name)
        shown = "set (env)" if cls == "low" else f"set (env) — secret class {cls}, masked"
        record("D4", f"env:{name}", "info", f"{name} = {shown}",
               detail=f"secret_class={cls} known={is_known_var(name)}")
    if unknown:
        record(
            "D4", "env_vars", "warn",
            f"unknown TOKENPAK_* name(s): {', '.join(unknown)} (possible typo)",
            detail=(
                "Unknown names are honored as pass-through values (always-"
                "dynamic, spec §3.4) — reported here, never failed."
            ),
        )
    else:
        record("D4", "env_vars", "ok",
               f"{len(set_vars)} TokenPak/provider var(s) set; all names known",
               detail="validated against the Packet-A schema manifest (names only)")

    # === D5: ANTHROPIC_BASE_URL attach state ==================================
    env_base = os.environ.get("ANTHROPIC_BASE_URL", "").strip() or None
    settings_base = None
    claude_settings = Path.home() / ".claude" / "settings.json"
    if claude_settings.is_file():
        try:
            data = json.loads(claude_settings.read_text(encoding="utf-8"))
            raw = (data.get("env") or {}).get("ANTHROPIC_BASE_URL")
            settings_base = raw.strip() if isinstance(raw, str) and raw.strip() else None
        except Exception:
            settings_base = None

    def _is_local(url: str) -> bool:
        return "127.0.0.1" in url or "localhost" in url

    if env_base is None and settings_base is None:
        record("D5", "attach_state", "ok",
               "ANTHROPIC_BASE_URL not set (no proxy attach)",
               detail="neither os.environ nor ~/.claude/settings.json sets a base URL")
    elif env_base and settings_base and env_base.rstrip("/") != settings_base.rstrip("/"):
        record("D5", "attach_state", "warn",
               "ANTHROPIC_BASE_URL mismatch between env and client settings",
               detail="os.environ and ~/.claude/settings.json disagree — "
                      "stale attach likely; re-run `tokenpak integrate`")
    else:
        effective = env_base or settings_base or ""
        source = "env" if env_base else "~/.claude/settings.json"
        if _is_local(effective):
            record("D5", "attach_state", "ok",
                   f"attached to local proxy ({source})",
                   detail=f"base URL points at the local proxy (read from {source})")
        else:
            record("D5", "attach_state", "info",
                   f"pointed at non-default upstream ({source})",
                   detail="base URL is set but not the local proxy")

    # === D6: .env file hygiene ================================================
    d6_status, d6_msgs, d6_detail = "ok", [], []
    if home_env.is_file():
        mode = stat_mod.S_IMODE(home_env.stat().st_mode)
        if mode & 0o077:
            d6_status = "warn"
            d6_msgs.append(f"<tpk-home>/.env mode {oct(mode)} (expected 0600)")
            d6_detail.append(
                f"{home_env}: mode {oct(mode)} is looser than 0600 — advise "
                "`chmod 600` (doctor is read-only and will not fix)"
            )
        else:
            d6_detail.append(f"{home_env}: mode {oct(mode)} ok")
    else:
        d6_detail.append("<tpk-home>/.env absent")
    if (cwd / ".env").is_file():
        covered = _gitignore_covers_env(cwd)
        if covered is False:
            d6_status = "warn"
            d6_msgs.append("./.env not covered by ./.gitignore")
            d6_detail.append("./.env exists but .gitignore has no .env rule")
        else:
            d6_detail.append("./.env present" + (" (gitignored)" if covered else ""))
    else:
        d6_detail.append("./.env absent")
    record("D6", "env_file_hygiene", d6_status,
           "; ".join(d6_msgs) if d6_msgs else ".env hygiene ok (names only, never values)",
           detail="; ".join(d6_detail))

    # === D7: user/system boundary drift =======================================
    d7_findings: list[tuple[str, str, str]] = []  # (status, message, detail)
    d7_notes: list[str] = []
    if _paths.has_legacy() and _paths.has_canonical():
        d7_findings.append((
            "warn", "split-home: both ~/.tpk/ and ~/.tokenpak/ exist",
            f"canonical {_paths.canonical_home()} wins; reconcile then remove "
            f"the legacy tree (Std 33 §8)",
        ))
    # Loader-drift (§0.2): the live loaders hardcode ~/.tokenpak/ while the
    # canonical resolver is _paths.home(). Re-derive their effective targets
    # (fresh, not the import-time constants) and compare. Repointing them is
    # a HELD build-time follow-up (§3.7) — doctor only surfaces the drift:
    # warn when user state actually lives at the divergent path (real split
    # risk), info-note when the divergence is latent.
    if "TOKENPAK_CONFIG" not in os.environ:
        loader_target = Path.home() / ".tokenpak" / "config.yaml"
        if loader_target.parent != home:
            if loader_target.exists():
                d7_findings.append((
                    "warn",
                    "loader drift: config_loader reads ~/.tokenpak/config.yaml, "
                    f"which exists, while canonical home is {home}",
                    "core/config.json toggles share the same hardcoded parent; "
                    "repointing onto _paths is a HELD follow-up (design §0.2/§3.7)",
                ))
            else:
                d7_notes.append(
                    "latent loader drift: config_loader targets "
                    "~/.tokenpak/config.yaml (absent) while canonical home is "
                    f"{home} (§0.2; repoint HELD per §3.7)"
                )
    tracked = _git_tracked_candidates(cwd)
    leaked: list[str] = []
    for path in tracked:
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for family, pattern in _SECRET_SHAPE_PATTERNS:
            if pattern.search(content):
                leaked.append(f"{path.relative_to(cwd)} ({family}-shaped)")
                break
    if leaked:
        d7_findings.append((
            "fail",
            f"secret-shaped value in tracked file(s): {', '.join(leaked)}",
            "A high-secret-class value (by shape) appears in a committed "
            "surface (Std 36 §1.4). Matched text is never echoed. Rotate "
            "the credential and purge the file from history.",
        ))
    if d7_findings:
        worst = "fail" if any(s == "fail" for s, _, _ in d7_findings) else "warn"
        record("D7", "boundary_drift", worst,
               "; ".join(m for _, m, _ in d7_findings),
               detail=" | ".join([d for _, _, d in d7_findings] + d7_notes))
    else:
        record("D7", "boundary_drift", "ok",
               "single home, no active loader drift, no committed secrets",
               detail="; ".join(
                   [f"home={home}; tracked candidates scanned: {len(tracked)}"]
                   + d7_notes
               ))

    # === D8: schema coverage (info only — never blocks) =======================
    documented = [n for n in set_vars if is_known_var(n)]
    deprecated = [
        n for n in set_vars
        if n.startswith(_OPENCLAW_PREFIX) or n == "TOKENPAK_OPENCLAW_FALLBACK"
    ]
    d8_detail = (
        f"set+documented={len(documented)}; set+undocumented={len(unknown)}"
        + (f"; deprecating (v1.7→v1.9): {', '.join(deprecated)}" if deprecated else "")
    )
    record("D8", "schema_coverage", "info",
           f"schema coverage: {len(documented)}/{len(set_vars)} set var(s) documented",
           detail=d8_detail)

    # --- output ---------------------------------------------------------------
    exit_code = 4 if counts["fail"] else 0
    if json_output:
        print(json.dumps({
            "home": {"path": str(home), "rule": rule},
            "checks": checks,
            "summary": dict(counts),
            "exit_code": exit_code,
        }, indent=2))
        return exit_code

    renderers = {"ok": _Colors.ok, "warn": _Colors.warn,
                 "fail": _Colors.fail, "info": _Colors.info}
    if quiet:
        # Worst status line only; nothing on all-ok (Std 03 §4.3).
        for status in ("fail", "warn"):
            worst = [c for c in checks if c["status"] == status]
            if worst:
                print(renderers[status](worst[0]["message"]))
                break
        return exit_code

    print("\nTOKENPAK  |  Config Doctor")
    print("──────────────────────────────\n")
    for c in checks:
        line = f"[{c['id']}] {c['message']}"
        print(renderers[c["status"]](line))
        if verbose and c["detail"]:
            for ln in c["detail"].splitlines():
                print(f"         {ln}")
    print(
        f"\n  {counts['ok']} ok · {counts['warn']} warn · "
        f"{counts['fail']} fail · {counts['info']} info"
    )
    if counts["fail"]:
        print("  Result: config error (exit 4)")
    return exit_code
