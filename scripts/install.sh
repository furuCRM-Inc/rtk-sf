#!/usr/bin/env bash
# install.sh — One-command rtk-sf setup for Salesforce DX projects.
#
# Usage:
#   curl -sSL https://raw.githubusercontent.com/furuCRM-Inc/rtk-sf/main/scripts/install.sh | bash
#
# Or locally:
#   chmod +x scripts/install.sh && ./scripts/install.sh
#
# What this does:
#   1. Checks Python 3.9+
#   2. pip install rtk-sf[all]  (core + vector re-ranking + OCR)
#   3. python3 -m rtk_sf install (index + patch CLAUDE.md + print next steps)
#
# Requirements:
#   - Python 3.9+ with pip
#   - Salesforce DX project (force-app/ directory or similar)

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'

info()    { echo -e "${BLUE}[rtk-sf]${NC} $*"; }
success() { echo -e "${GREEN}[rtk-sf]${NC} $*"; }
error()   { echo -e "${RED}[rtk-sf] ERROR:${NC} $*" >&2; }
bold()    { echo -e "${BOLD}$*${NC}"; }

RTK_REPO="https://github.com/furuCRM-Inc/rtk-sf.git"

# ---------------------------------------------------------------------------
# Step 1: Check Python version
# ---------------------------------------------------------------------------

# Run a Python command with a 5-second timeout.
# Usage: _py_timeout <python_cmd> <args...>
_py_timeout() {
  local cmd="$1"; shift
  if command -v timeout &>/dev/null; then
    timeout 5 "$cmd" "$@"
  else
    # Fallback: background process + manual kill
    "$cmd" "$@" &
    local pid=$!
    local i=0
    while kill -0 "$pid" 2>/dev/null && [ $i -lt 5 ]; do
      sleep 1; i=$((i+1))
    done
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null
      return 1
    fi
    wait "$pid"
  fi
}

# Detect Microsoft Store Python stub (Windows/Git Bash).
# The stub silently opens the Store instead of running Python — causes hangs.
_check_ms_store_stub() {
  local py_path
  py_path=$(command -v "$1" 2>/dev/null || true)
  if echo "$py_path" | grep -qi "WindowsApps"; then
    echo ""
    error "'$1' points to a Microsoft Store stub, not a real Python installation."
    error "  Detected path: $py_path"
    echo ""
    echo "  Fix (2 steps):"
    echo "  1. Windows Settings → Apps → Advanced app settings → App execution aliases"
    echo "     → turn OFF 'python.exe' and 'python3.exe'"
    echo "  2. Install real Python from https://python.org/downloads"
    echo "     (check 'Add Python to PATH' during setup)"
    echo ""
    exit 1
  fi
}

check_python() {
  info "Checking Python version..."

  if command -v python3 &>/dev/null; then
    _check_ms_store_stub python3
    PYTHON_CMD="python3"
  elif command -v python &>/dev/null; then
    _check_ms_store_stub python
    PYTHON_CMD="python"
  else
    error "Python not found. Install Python 3.9+ from https://python.org/downloads"
    if echo "$OSTYPE" | grep -qi "msys\|cygwin\|win"; then
      error "(Windows: check 'Add Python to PATH' during installation)"
    fi
    exit 1
  fi

  # Run with timeout — a Store stub or broken install can hang indefinitely
  PYTHON_VERSION=$(_py_timeout "$PYTHON_CMD" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null) || {
    error "'$PYTHON_CMD' timed out or failed to run."
    error "This usually means it is a Microsoft Store stub or a broken install."
    error "Fix: https://python.org/downloads (check 'Add Python to PATH')"
    error "     Then disable python.exe in Windows Settings > App execution aliases."
    exit 1
  }

  PYTHON_MAJOR="${PYTHON_VERSION%%.*}"
  PYTHON_MINOR="${PYTHON_VERSION##*.}"

  if [ "$PYTHON_MAJOR" -lt 3 ] || { [ "$PYTHON_MAJOR" -eq 3 ] && [ "$PYTHON_MINOR" -lt 9 ]; }; then
    error "Python 3.9+ required. Found: $PYTHON_VERSION"
    error "Upgrade: https://python.org/downloads"
    exit 1
  fi

  success "Python $PYTHON_VERSION found at: $(command -v "$PYTHON_CMD")"
}

# ---------------------------------------------------------------------------
# Step 2: Ensure pip >= 22 (required for PEP 508 "name @ git+url" syntax)
# ---------------------------------------------------------------------------
upgrade_pip() {
  PIP_VERSION=$("$PYTHON_CMD" -m pip --version 2>/dev/null | awk '{print $2}')
  PIP_MAJOR="${PIP_VERSION%%.*}"
  if [ -z "$PIP_MAJOR" ] || [ "$PIP_MAJOR" -lt 22 ]; then
    info "pip $PIP_VERSION is too old (need 22+). Upgrading pip..."
    "$PYTHON_CMD" -m pip install --quiet --upgrade pip || {
      error "Failed to upgrade pip. Try: $PYTHON_CMD -m pip install --upgrade pip"
      exit 1
    }
    success "pip upgraded."
  fi
}

# ---------------------------------------------------------------------------
# Step 3: pip install rtk-sf[all]
# ---------------------------------------------------------------------------
install_rtk_sf() {
  info "Installing rtk-sf[all] from GitHub..."

  if "$PYTHON_CMD" -m pip install --quiet "rtk-sf[all] @ git+${RTK_REPO}@main" 2>&1; then
    RTK_VERSION=$("$PYTHON_CMD" -m rtk_sf --version 2>/dev/null || echo "unknown")
    success "rtk-sf $RTK_VERSION installed (core + vector re-ranking + OCR)."
  else
    error "pip install failed."
    error "Try manually: pip install \"rtk-sf[all] @ git+${RTK_REPO}@main\""
    exit 1
  fi
}

# ---------------------------------------------------------------------------
# Step 3: Delegate everything else to the Python CLI
# ---------------------------------------------------------------------------
run_setup() {
  "$PYTHON_CMD" -m rtk_sf install
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
main() {
  echo ""
  bold "rtk-sf installer"
  bold "Zero-Token Knowledge Layer for Salesforce AI Agents"
  echo ""

  check_python
  upgrade_pip
  install_rtk_sf
  run_setup
}

main "$@"
