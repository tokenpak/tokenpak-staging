#!/usr/bin/env bash
# tokenpak-install.sh — Premium Installation Experience
# Usage: bash tokenpak-install.sh [--uninstall] [--dry-run] [--help]
#
# One-liner install:
#   curl -sSL https://tokenpak.dev/install | bash
#   OR: tokenpak install

set -euo pipefail

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CONFIGURATION
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TOKENPAK_VERSION="${TOKENPAK_VERSION:-latest}"
PROXY_PORT="${TOKENPAK_PORT:-8766}"
TOKENPAK_CONFIG_DIR="$HOME/.tokenpak"
OPENCLAW_CONFIG="$HOME/.openclaw/openclaw.json"
BACKUP_SUFFIX=".bak.$(date +%Y%m%d-%H%M%S)"

# Flags
DRY_RUN=false
UNINSTALL=false
VERBOSE=false

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# COLORS (respect NO_COLOR)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
if [ -z "${NO_COLOR:-}" ] && [ -t 1 ]; then
  BOLD="\033[1m"
  DIM="\033[2m"
  RED="\033[31m"
  GREEN="\033[32m"
  YELLOW="\033[33m"
  BLUE="\033[34m"
  CYAN="\033[36m"
  RESET="\033[0m"
else
  BOLD="" DIM="" RED="" GREEN="" YELLOW="" BLUE="" CYAN="" RESET=""
fi

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# HELPERS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_hr()   { printf "${DIM}%-38s${RESET}\n" "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"; }
_ok()   { printf "  ${GREEN}✓${RESET} %-24s %s\n" "$1" "$2"; }
_warn() { printf "  ${YELLOW}⚠${RESET} %-24s %s\n" "$1" "$2"; }
_fail() { printf "  ${RED}✗${RESET} %-24s %s\n" "$1" "$2"; }
_info() { printf "  ${CYAN}→${RESET} %s\n" "$1"; }
_step() { printf "\n${BOLD}%s${RESET}\n" "$1"; _hr; }
_dry()  { $DRY_RUN && printf "  ${YELLOW}[dry-run]${RESET} %s\n" "$1" && return 0; return 1; }
_die()  { printf "\n${RED}✗ Error:${RESET} %s\n\n" "$1" >&2; exit 1; }

spin_pid=""
_spin_start() {
  if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    { i=0; chars="⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
      while true; do
        printf "\r  ${CYAN}%s${RESET} %s" "${chars:$i:1}" "$1"
        i=$(( (i+1) % 10 ))
        sleep 0.1
      done
    } &
    spin_pid=$!
  fi
}
_spin_stop() {
  if [ -n "$spin_pid" ] 2>/dev/null; then
    kill "$spin_pid" 2>/dev/null || true
    wait "$spin_pid" 2>/dev/null || true
    spin_pid=""
    printf "\r%60s\r" ""  # clear spinner line
  fi
}

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# PARSE ARGS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
for arg in "$@"; do
  case "$arg" in
    --uninstall) UNINSTALL=true ;;
    --dry-run)   DRY_RUN=true ;;
    --verbose)   VERBOSE=true ;;
    --help|-h)
      cat <<HELPEOF
TokenPak Installer

Usage:
  bash tokenpak-install.sh           Install TokenPak
  bash tokenpak-install.sh --help    Show this help
  bash tokenpak-install.sh --dry-run Show what would happen (no changes)
  bash tokenpak-install.sh --uninstall Remove TokenPak

Environment variables:
  NO_COLOR=1        Disable colored output
  TOKENPAK_PORT     Proxy port (default: 8766)
  TOKENPAK_VERSION  Version to install (default: latest)

HELPEOF
      exit 0
      ;;
    *) _die "Unknown argument: $arg. Run with --help for usage." ;;
  esac
done

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# UNINSTALL
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
do_uninstall() {
  printf "\n${BOLD}🗑  TokenPak Uninstall${RESET}\n"
  _hr

  # Stop proxy if running
  if command -v tokenpak >/dev/null 2>&1; then
    _spin_start "Stopping proxy..."
    tokenpak stop >/dev/null 2>&1 || true
    _spin_stop
    _ok "Proxy" "stopped"
  fi

  # Remove systemd service
  if systemctl --user list-unit-files tokenpak-proxy.service >/dev/null 2>&1; then
    _dry "systemctl --user disable --now tokenpak-proxy.service" || {
      systemctl --user disable --now tokenpak-proxy.service >/dev/null 2>&1 || true
      rm -f "$HOME/.config/systemd/user/tokenpak-proxy.service" 2>/dev/null || true
      systemctl --user daemon-reload >/dev/null 2>&1 || true
      _ok "systemd service" "removed"
    }
  fi

  # Backup config before removal
  if [ -d "$TOKENPAK_CONFIG_DIR" ]; then
    backup_path="${TOKENPAK_CONFIG_DIR}${BACKUP_SUFFIX}"
    _dry "mv $TOKENPAK_CONFIG_DIR $backup_path" || {
      mv "$TOKENPAK_CONFIG_DIR" "$backup_path"
      _ok "Config backed up" "$backup_path"
    }
  fi

  # Remove pip package
  _spin_start "Removing tokenpak package..."
  _dry "pip uninstall -y tokenpak" || {
    pip uninstall -y tokenpak >/dev/null 2>&1 || true
  }
  _spin_stop
  _ok "Package" "removed"

  printf "\n${GREEN}✓ TokenPak uninstalled.${RESET}\n"
  printf "  Config backup: %s\n\n" "${TOKENPAK_CONFIG_DIR}${BACKUP_SUFFIX}"
}

if $UNINSTALL; then
  do_uninstall
  exit 0
fi

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# HEADER
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
printf "\n${BOLD}🚀 TokenPak Installation${RESET}\n"
_hr
$DRY_RUN && printf "  ${YELLOW}Running in dry-run mode — no changes will be made${RESET}\n\n"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# STEP 1: ENVIRONMENT DETECTION
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_step "📋 Environment"

# Detect OS
OS_NAME="unknown"
OS_DETAIL=""
if [ "$(uname)" = "Darwin" ]; then
  OS_NAME="macOS"
  OS_DETAIL="$(sw_vers -productVersion 2>/dev/null || echo '')"
  IS_LINUX=false
elif grep -qi microsoft /proc/version 2>/dev/null; then
  OS_NAME="WSL"
  OS_DETAIL="$(uname -r | cut -d- -f1)"
  IS_LINUX=true
elif [ "$(uname)" = "Linux" ]; then
  OS_NAME="Linux"
  OS_DETAIL="$(. /etc/os-release 2>/dev/null && echo "$NAME $VERSION_ID" || uname -r)"
  IS_LINUX=true
fi
_ok "OS" "$OS_NAME $OS_DETAIL"

# Detect OpenClaw
IS_OPENCLAW=false
OPENCLAW_AGENTS=""
if [ -f "$OPENCLAW_CONFIG" ]; then
  IS_OPENCLAW=true
  # Try to detect agent name from whoami
  AGENT_NAME="$(whoami)"
  _ok "Mode" "OpenClaw agent ($AGENT_NAME)"
else
  _ok "Mode" "Standalone"
fi

# Detect existing install
EXISTING_VERSION=""
if command -v tokenpak >/dev/null 2>&1; then
  EXISTING_VERSION="$(tokenpak version 2>/dev/null | head -1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' || echo 'unknown')"
  _ok "Existing install" "v$EXISTING_VERSION (upgrading)"
  IS_UPGRADE=true
else
  _ok "Install type" "Fresh installation"
  IS_UPGRADE=false
fi

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# STEP 2: PREREQUISITES CHECK
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_step "🔍 Prerequisites"

PREFLIGHT_OK=true

# Python version
PYTHON_CMD=""
for cmd in python3 python; do
  if command -v "$cmd" >/dev/null 2>&1; then
    ver="$($cmd --version 2>&1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+')"
    major="${ver%%.*}"
    minor="${ver#*.}"; minor="${minor%%.*}"
    if [ "$major" -ge 3 ] && [ "$minor" -ge 10 ]; then
      PYTHON_CMD="$cmd"
      _ok "Python" "$ver (required 3.10+)"
      break
    else
      _fail "Python" "$ver — need 3.10+ (found $cmd)"
      PREFLIGHT_OK=false
    fi
  fi
done
if [ -z "$PYTHON_CMD" ]; then
  _fail "Python" "not found — install Python 3.10+ first"
  PREFLIGHT_OK=false
fi

# pip
if [ -n "$PYTHON_CMD" ] && "$PYTHON_CMD" -m pip --version >/dev/null 2>&1; then
  _ok "pip" "available"
else
  _fail "pip" "not found — run: $PYTHON_CMD -m ensurepip"
  PREFLIGHT_OK=false
fi

# Disk space (need at least 100MB)
if command -v df >/dev/null 2>&1; then
  FREE_MB="$(df -m "$HOME" 2>/dev/null | awk 'NR==2{print $4}')"
  if [ -n "$FREE_MB" ] && [ "$FREE_MB" -ge 100 ]; then
    _ok "Disk space" "${FREE_MB}MB free (required 100MB)"
  elif [ -n "$FREE_MB" ]; then
    _fail "Disk space" "${FREE_MB}MB free — need at least 100MB"
    PREFLIGHT_OK=false
  fi
fi

# Network
if command -v curl >/dev/null 2>&1; then
  if curl -s --connect-timeout 3 https://pypi.org >/dev/null 2>&1; then
    _ok "Network" "Connected (PyPI reachable)"
  else
    _warn "Network" "Cannot reach PyPI — install may fail"
  fi
fi

# Systemd (Linux only)
HAS_SYSTEMD=false
if $IS_LINUX && systemctl --user status >/dev/null 2>&1; then
  HAS_SYSTEMD=true
  _ok "systemd" "user mode available"
fi

if ! $PREFLIGHT_OK; then
  _die "Prerequisites check failed. Fix the issues above and re-run."
fi

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# STEP 3: API KEY DISCOVERY
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_step "🔑 API Keys"

FOUND_KEYS=""
# Check environment
[ -n "${ANTHROPIC_API_KEY:-}" ] && { _ok "Anthropic" "found (ANTHROPIC_API_KEY)"; FOUND_KEYS="$FOUND_KEYS anthropic"; }
[ -n "${OPENAI_API_KEY:-}" ]    && { _ok "OpenAI"    "found (OPENAI_API_KEY)"; FOUND_KEYS="$FOUND_KEYS openai"; }
[ -n "${GOOGLE_API_KEY:-}" ]    && { _ok "Google"    "found (GOOGLE_API_KEY)"; FOUND_KEYS="$FOUND_KEYS google"; }

# Check OpenClaw config
if [ -f "$OPENCLAW_CONFIG" ] && command -v python3 >/dev/null 2>&1; then
  OC_KEYS="$(python3 -c "
import json, sys
try:
    d = json.load(open('$OPENCLAW_CONFIG'))
    providers = d.get('providers', {})
    found = []
    for name, cfg in providers.items():
        if isinstance(cfg, dict) and cfg.get('apiKey') or cfg.get('api_key'):
            found.append(name)
    print(' '.join(found))
except Exception:
    pass
" 2>/dev/null || true)"
  for key in $OC_KEYS; do
    echo "$FOUND_KEYS" | grep -q "$key" || {
      _ok "$key" "found in OpenClaw config"
      FOUND_KEYS="$FOUND_KEYS $key"
    }
  done
fi

# Scan shell profiles
for profile in "$HOME/.bashrc" "$HOME/.bash_profile" "$HOME/.zshenv" "$HOME/.zshrc" "$HOME/.profile"; do
  if [ -f "$profile" ]; then
    if grep -q "ANTHROPIC_API_KEY\|OPENAI_API_KEY\|GOOGLE_API_KEY" "$profile" 2>/dev/null; then
      _info "API keys detected in $profile (source it to make them active)"
    fi
  fi
done

if [ -z "$FOUND_KEYS" ]; then
  _warn "API keys" "none found — set ANTHROPIC_API_KEY or OPENAI_API_KEY"
  _info "Example: export ANTHROPIC_API_KEY='sk-ant-...'"
  _info "TokenPak will install, but won't be able to proxy requests yet"
fi

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# STEP 4: INSTALLATION
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_step "📦 Installation"

if $IS_UPGRADE; then
  INSTALL_CMD="pip install --upgrade tokenpak"
else
  INSTALL_CMD="pip install tokenpak"
fi

_dry "$INSTALL_CMD" || {
  _spin_start "Installing TokenPak via pip..."
  if "$PYTHON_CMD" -m pip install --upgrade tokenpak >/dev/null 2>&1; then
    _spin_stop
    NEW_VER="$(tokenpak version 2>/dev/null | head -1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' || echo 'latest')"
    if $IS_UPGRADE; then
      _ok "Package" "upgraded v$EXISTING_VERSION → v$NEW_VER"
    else
      _ok "Package" "installed v$NEW_VER"
    fi
  else
    _spin_stop
    _fail "Package" "pip install failed"
    _info "Try: $PYTHON_CMD -m pip install tokenpak --verbose"
    exit 1
  fi
}

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# STEP 5: CONFIGURATION
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_step "⚙️  Configuration"

CONFIG_FILE="$TOKENPAK_CONFIG_DIR/config.yaml"

if $IS_UPGRADE && [ -f "$CONFIG_FILE" ]; then
  _ok "Config" "preserved existing config"
else
  _dry "tokenpak init --for openclaw / --for standalone" || {
    _spin_start "Generating config..."
    if $IS_OPENCLAW; then
      tokenpak init --for openclaw >/dev/null 2>&1 || tokenpak setup >/dev/null 2>&1 || true
    else
      tokenpak init >/dev/null 2>&1 || tokenpak setup >/dev/null 2>&1 || true
    fi
    _spin_stop
    [ -f "$CONFIG_FILE" ] && _ok "Config" "generated at $CONFIG_FILE" || _warn "Config" "use 'tokenpak init' to configure"
  }
fi

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# STEP 6: OPENCLAW / SYSTEMD SETUP
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
if $IS_OPENCLAW && $HAS_SYSTEMD; then
  _step "🔧 OpenClaw Integration (systemd)"

  SERVICE_DIR="$HOME/.config/systemd/user"
  SERVICE_FILE="$SERVICE_DIR/tokenpak-proxy.service"

  _dry "create systemd service" || {
    mkdir -p "$SERVICE_DIR"
    TOKENPAK_BIN="$(command -v tokenpak)"
    cat > "$SERVICE_FILE" <<SERVICE
[Unit]
Description=TokenPak LLM Proxy
After=network.target

[Service]
Type=simple
ExecStartPre=${HOME}/.local/bin/tokenpak-inject.sh
ExecStart=${TOKENPAK_BIN} start --no-daemon
Restart=on-failure
RestartSec=5s
Environment=TOKENPAK_PORT=${PROXY_PORT}

[Install]
WantedBy=default.target
SERVICE

    _ok "systemd service" "created at $SERVICE_FILE"

    _spin_start "Enabling autostart..."
    systemctl --user daemon-reload >/dev/null 2>&1 || true
    systemctl --user enable tokenpak-proxy.service >/dev/null 2>&1 || true
    _spin_stop
    _ok "Autostart" "enabled (starts on login)"

    _spin_start "Starting proxy..."
    systemctl --user restart tokenpak-proxy.service >/dev/null 2>&1 || true
    sleep 2
    _spin_stop
    if systemctl --user is-active --quiet tokenpak-proxy.service 2>/dev/null; then
      _ok "Proxy service" "running"
    else
      _warn "Proxy service" "may not be running (check: systemctl --user status tokenpak-proxy)"
    fi
  }
elif ! $IS_OPENCLAW; then
  _step "🚀 Starting Proxy"
  _dry "tokenpak start" || {
    _spin_start "Starting proxy on port $PROXY_PORT..."
    tokenpak start >/dev/null 2>&1 &
    sleep 2
    _spin_stop
    _ok "Proxy" "started on port $PROXY_PORT"
  }
fi

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# STEP 7: HEALTH CHECK
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_step "✅ Validation"

if $DRY_RUN; then
  _ok "Proxy check" "[skipped in dry-run]"
  _ok "Config check" "[skipped in dry-run]"
  _ok "API connectivity" "[skipped in dry-run]"
else
  # Proxy health
  HEALTH_OK=false
  for i in 1 2 3; do
    if curl -s --connect-timeout 2 "http://localhost:${PROXY_PORT}/health" >/dev/null 2>&1 || \
       curl -s --connect-timeout 2 "http://localhost:${PROXY_PORT}/stats" >/dev/null 2>&1; then
      HEALTH_OK=true
      LATENCY="$(curl -s -o /dev/null -w '%{time_total}' "http://localhost:${PROXY_PORT}/stats" 2>/dev/null | awk '{printf "%dms", $1*1000}' || echo '?')"
      _ok "Proxy responding" "http://localhost:${PROXY_PORT} (latency: $LATENCY)"
      break
    fi
    sleep 1
  done
  $HEALTH_OK || _warn "Proxy" "not responding — run 'tokenpak start' manually"

  # Config valid
  [ -f "$CONFIG_FILE" ] && _ok "Config" "valid at $CONFIG_FILE" || _warn "Config" "not found — run 'tokenpak init'"

  # API keys summary
  KEY_COUNT=0
  for k in ANTHROPIC_API_KEY OPENAI_API_KEY GOOGLE_API_KEY; do
    eval "val=\${$k:-}"
    [ -n "$val" ] && KEY_COUNT=$((KEY_COUNT + 1))
  done
  if [ "$KEY_COUNT" -gt 0 ]; then
    _ok "API keys" "$KEY_COUNT provider(s) configured"
  else
    _warn "API keys" "none set — proxy will not route requests"
    _info "Set ANTHROPIC_API_KEY or OPENAI_API_KEY in your shell profile"
  fi
fi

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# DONE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
printf "\n${GREEN}${BOLD}🎉 TokenPak is ready!${RESET}\n"
_hr
printf "\nNext steps:\n"
printf "  • View config:    ${CYAN}tokenpak config show${RESET}\n"
printf "  • Run health check: ${CYAN}tokenpak verify${RESET}\n"
printf "  • Test proxy:     ${CYAN}curl http://localhost:${PROXY_PORT}/stats${RESET}\n"
if $IS_OPENCLAW; then
  printf "  • Start OpenClaw: ${CYAN}openclaw gateway start${RESET}\n"
fi
printf "\nLearn more:  https://tokenpak.dev/docs\n\n"
