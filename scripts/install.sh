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
#   3. python -m rtk_sf install (index + patch CLAUDE.md + print next steps)
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
check_python() {
  info "Checking Python version..."

  if command -v python3 &>/dev/null; then
    PYTHON_CMD="python3"
  elif command -v python &>/dev/null; then
    PYTHON_CMD="python"
  else
    error "Python not found. Please install Python 3.9+ from https://python.org"
    exit 1
  fi

  PYTHON_VERSION=$("$PYTHON_CMD" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
  PYTHON_MAJOR=$("$PYTHON_CMD" -c "import sys; print(sys.version_info.major)")
  PYTHON_MINOR=$("$PYTHON_CMD" -c "import sys; print(sys.version_info.minor)")

  if [ "$PYTHON_MAJOR" -lt 3 ] || { [ "$PYTHON_MAJOR" -eq 3 ] && [ "$PYTHON_MINOR" -lt 9 ]; }; then
    error "Python 3.9+ required. Found: $PYTHON_VERSION"
    error "Please upgrade Python: https://python.org/downloads"
    exit 1
  fi

  success "Python $PYTHON_VERSION found."
}

# ---------------------------------------------------------------------------
# Step 2: pip install rtk-sf[all]
# ---------------------------------------------------------------------------
install_rtk_sf() {
  info "Installing rtk-sf[all] from GitHub..."

  if "$PYTHON_CMD" -m pip install --quiet "git+${RTK_REPO}@main#egg=rtk-sf[all]" 2>&1; then
    RTK_VERSION=$("$PYTHON_CMD" -m rtk_sf --version 2>/dev/null || echo "unknown")
    success "rtk-sf $RTK_VERSION installed (core + vector re-ranking + OCR)."
  else
    error "pip install failed."
    error "Try manually: pip install \"git+${RTK_REPO}@main#egg=rtk-sf[all]\""
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
  install_rtk_sf
  run_setup
}

main "$@"
