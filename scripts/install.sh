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
#   2. Installs rtk-sf via pip
#   3. Indexes the current directory (Salesforce DX project)
#   4. Prints MCP setup instructions
#
# Requirements:
#   - Python 3.9+ with pip
#   - Salesforce DX project (force-app/ directory or similar)
#   - curl (for remote install only)

set -euo pipefail

# ---------------------------------------------------------------------------
# Colors
# ---------------------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
BOLD='\033[1m'
NC='\033[0m' # No Color

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
info()    { echo -e "${BLUE}[rtk-sf]${NC} $*"; }
success() { echo -e "${GREEN}[rtk-sf]${NC} $*"; }
warn()    { echo -e "${YELLOW}[rtk-sf]${NC} $*"; }
error()   { echo -e "${RED}[rtk-sf] ERROR:${NC} $*" >&2; }
bold()    { echo -e "${BOLD}$*${NC}"; }

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
# Step 2: Install rtk-sf
# ---------------------------------------------------------------------------
install_rtk_sf() {
  info "Installing rtk-sf..."

  if "$PYTHON_CMD" -m pip install --quiet rtk-sf 2>&1; then
    success "rtk-sf installed successfully."
  else
    error "pip install failed."
    error "Try manually: pip install rtk-sf"
    exit 1
  fi

  # Verify installation
  RTK_VERSION=$("$PYTHON_CMD" -m rtk_sf --version 2>/dev/null || echo "unknown")
  success "rtk-sf $RTK_VERSION ready."
}

# ---------------------------------------------------------------------------
# Step 3: Detect Salesforce project
# ---------------------------------------------------------------------------
detect_project() {
  info "Detecting Salesforce DX project..."

  if [ -d "force-app" ]; then
    success "force-app/ directory found."
    SF_SOURCE_DIR="force-app"
  elif [ -d "src" ] && find src -name "*.cls" -maxdepth 5 | grep -q .; then
    warn "No force-app/ directory; using src/ (non-standard layout)."
    SF_SOURCE_DIR="src"
  elif find . -name "*.cls" -maxdepth 8 | grep -q .; then
    warn "No force-app/ directory; scanning project root."
    SF_SOURCE_DIR="."
  else
    warn "No Apex (.cls) files found in the current directory."
    warn "Make sure you're running this from your Salesforce DX project root."
    SF_SOURCE_DIR="force-app"
  fi
}

# ---------------------------------------------------------------------------
# Step 4: Run initial index
# ---------------------------------------------------------------------------
run_index() {
  info "Indexing Salesforce metadata in ${SF_SOURCE_DIR}/..."
  echo ""

  if "$PYTHON_CMD" -m rtk_sf index --path "./$SF_SOURCE_DIR"; then
    echo ""
    success "Index complete. Specs stored in .rtk-sf/"
  else
    warn "Indexing encountered errors. Check the output above."
    warn "You can re-run: rtk-sf index"
  fi
}

# ---------------------------------------------------------------------------
# Step 5: Patch CLAUDE.md so Claude Code uses rtk-sf tools automatically
# ---------------------------------------------------------------------------
RTK_CLAUDE_MARKER="## Code Search"
RTK_CLAUDE_MARKER_V4="get_class_skeleton"

patch_claude_md() {
  local claude_md="CLAUDE.md"

  # Already on v0.4.0+ — skip
  if [ -f "$claude_md" ] && grep -q "$RTK_CLAUDE_MARKER_V4" "$claude_md" 2>/dev/null; then
    success "CLAUDE.md already has rtk-sf v0.4 instructions. Skipping."
    return
  fi

  # v0.3.0 block present — remove it so we can replace with v0.4 block
  if [ -f "$claude_md" ] && grep -q "$RTK_CLAUDE_MARKER" "$claude_md" 2>/dev/null; then
    warn "Upgrading CLAUDE.md from rtk-sf v0.3 → v0.4 tool list..."
    # Remove the old rtk-sf block (from marker line to next ## heading or EOF)
    local tmp
    tmp=$(mktemp)
    awk '/## Code Search/{found=1} found && /^## / && !/## Code Search/{found=0} !found' "$claude_md" > "$tmp"
    mv "$tmp" "$claude_md"
  fi

  local block
  block=$(cat <<'BLOCK'

## Code Search & Data — Use rtk-sf First (Required)

This project is indexed by **rtk-sf**. Always use the MCP tools before reading raw files or calling sf CLI:

| Task | Tool to call |
|---|---|
| Find a component by name or keyword | `search_codebase(query)` |
| Read a component's spec / fields / methods | `query_compressed_spec(component_name)` |
| Blast-radius before editing | `get_relations(component_name)` |
| List all Apex classes / objects / flows | `list_components(type)` |
| Write discovered business logic back | `annotate_component(component_name, key, value)` |
| Read an Apex class before editing (surgical) | `get_class_skeleton(component_name, focus_methods)` |
| Deploy / retrieve / run tests silently | `sf_command(action, target_org, ...)` |
| Get object field list for data creation | `get_object_schema(object_name)` |
| Inspect existing records (sample only) | `soql_query(query, target_org, sample_size)` |

**Never** do these directly — use the tool instead:
- ❌ `Read` a raw `.cls` file → ✅ `get_class_skeleton`
- ❌ `sf sobject describe` → ✅ `get_object_schema`
- ❌ `sf data query` → ✅ `soql_query`
- ❌ `sf project deploy start` → ✅ `sf_command(action="deploy")`

If search returns no results, re-index with:
```bash
python3 -m rtk_sf index
```
Do NOT use `npx rtk-sf` — rtk-sf is a Python package, not npm.

BLOCK
)

  if [ -f "$claude_md" ]; then
    # Prepend block after the first line (title) so it appears near the top
    local first_line
    first_line=$(head -1 "$claude_md")
    local rest
    rest=$(tail -n +2 "$claude_md")
    printf '%s\n%s\n%s\n' "$first_line" "$block" "$rest" > "$claude_md"
    success "CLAUDE.md updated with rtk-sf tool instructions."
  else
    # No CLAUDE.md yet — create a minimal one
    printf '# Salesforce Project\n%s\n' "$block" > "$claude_md"
    success "CLAUDE.md created with rtk-sf tool instructions."
  fi
}

# ---------------------------------------------------------------------------
# Step 6: Print next steps
# ---------------------------------------------------------------------------
print_next_steps() {
  echo ""
  bold "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  bold "  rtk-sf is ready!"
  bold "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo ""
  info "Next steps:"
  echo ""
  echo "  1. Register with Claude Code:"
  echo -e "     ${BOLD}claude mcp add rtk-sf -- python -m rtk_sf serve${NC}"
  echo ""
  echo "  2. Generate the visual architecture map:"
  echo -e "     ${BOLD}rtk-sf ui${NC}"
  echo -e "     ${BOLD}open dist/architecture_map.html${NC}"
  echo ""
  echo "  3. Enable live file watching during development:"
  echo -e "     ${BOLD}rtk-sf watch${NC}"
  echo ""
  echo "  4. Re-index after adding new Apex classes or objects:"
  echo -e "     ${BOLD}rtk-sf index${NC}"
  echo ""
  info "Docs: https://github.com/furuCRM-Inc/rtk-sf"
  echo ""
  echo -e "Built with love by ${BOLD}furuCRM Inc.${NC} — https://www.furucrm.com"
  echo ""
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
  detect_project
  run_index
  patch_claude_md
  print_next_steps
}

main "$@"
