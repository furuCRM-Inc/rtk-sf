# Installation Guide

## Prerequisites

- **Python 3.9+** — [python.org/downloads](https://python.org/downloads)
- **pip** — included with Python 3.4+
- **Salesforce DX project** with a `force-app/` directory (or equivalent source directory)

---

## macOS

### Option A: One-command installer (recommended)

```bash
cd /path/to/your/salesforce-project
curl -sSL https://raw.githubusercontent.com/furuCRM-Inc/rtk-sf/main/scripts/install.sh | bash
```

### Option B: Manual

```bash
# 1. Install rtk-sf
pip install rtk-sf

# 2. Navigate to your Salesforce project
cd /path/to/your/salesforce-project

# 3. Index the project
rtk-sf index

# 4. Register with Claude Code
claude mcp add rtk-sf -- python -m rtk_sf serve
```

### Verify

```bash
rtk-sf --version
# rtk-sf 0.1.0

rtk-sf index --help
```

---

## Linux (Ubuntu/Debian)

```bash
# Ensure Python 3.9+
python3 --version

# Install
pip3 install rtk-sf

# Or with explicit Python version
python3.11 -m pip install rtk-sf

# Index your project
cd /path/to/salesforce-project
rtk-sf index
```

If `rtk-sf` is not on your PATH after pip install, use:

```bash
python3 -m rtk_sf index
```

---

## Linux (RHEL/CentOS/Fedora)

```bash
# Python 3.9 may need to be installed separately
sudo dnf install python3.9   # Fedora
# or
sudo yum install python39     # RHEL/CentOS

python3.9 -m pip install rtk-sf
python3.9 -m rtk_sf index
```

---

## Windows

### PowerShell

```powershell
# Install
pip install rtk-sf

# Navigate to project
cd C:\path\to\your\salesforce-project

# Index
rtk-sf index

# Or using python -m if rtk-sf is not on PATH
python -m rtk_sf index
```

### Windows Subsystem for Linux (WSL2) — Recommended

```bash
# Install rtk-sf in WSL
pip install rtk-sf

# Mount Windows project (if needed)
cd /mnt/c/path/to/salesforce-project

# Index
rtk-sf index
```

### MCP server on Windows

```powershell
# Register with Claude Code (PowerShell)
claude mcp add rtk-sf -- python -m rtk_sf serve
```

---

## Virtual Environment (recommended for teams)

```bash
# Create venv in project root
python3 -m venv .venv

# Activate
source .venv/bin/activate      # macOS/Linux
.venv\Scripts\activate         # Windows

# Install
pip install rtk-sf

# Index
rtk-sf index

# Add the venv python to MCP config so it persists
which python  # e.g. /path/to/project/.venv/bin/python
claude mcp add rtk-sf -- /path/to/project/.venv/bin/python -m rtk_sf serve
```

---

## Optional: Vector Re-Ranking

Install numpy to enable cosine similarity re-ranking of search results:

```bash
pip install "rtk-sf[vector]"
# or
pip install rtk-sf numpy
```

When numpy is available, `search_codebase` results are re-ranked by semantic
similarity in addition to FTS5 keyword relevance.

---

## First Run

After installation, navigate to your Salesforce DX project root and run:

```bash
rtk-sf index
```

Expected output:
```
rtk-sf indexer starting...
  Project root : /projects/my-org
  Source path  : /projects/my-org/force-app
Indexing complete:
  Indexed : 84
  Skipped : 0 (unchanged)
  Errors  : 0
Populating search database...
  Synced 84 components into search index.
Index stored in: /projects/my-org/.rtk-sf
Run `rtk-sf serve` to start the MCP server.
Run `rtk-sf ui` to generate the architecture map.
```

The `.rtk-sf/` directory created in your project root contains:
- `registry.json` — mtime tracker
- `db.sqlite` — FTS5 search database
- `relations.json` — component graph
- `specs/*.yaml` — compressed YAML specs

Add `.rtk-sf/` to your `.gitignore` (it is project-local and rebuilt on demand):

```bash
echo ".rtk-sf/" >> .gitignore
echo "dist/architecture_map.html" >> .gitignore
```

---

## Claude Code Setup

```bash
# Register rtk-sf as an MCP server
claude mcp add rtk-sf -- python -m rtk_sf serve

# Verify
claude mcp list
# rtk-sf: python -m rtk_sf serve

# Test it — start a Claude Code session and ask:
# "Use query_compressed_spec to show me AccountService"
```

## Cline Setup

Add to `.cline/mcp.json` in your project, or to VS Code `settings.json`:

```json
{
  "cline.mcpServers": {
    "rtk-sf": {
      "command": "python",
      "args": ["-m", "rtk_sf", "serve"],
      "cwd": "${workspaceFolder}"
    }
  }
}
```

---

## Upgrading

```bash
pip install --upgrade rtk-sf

# Re-index after upgrade to pick up any spec format changes
rtk-sf index --force
```

---

## Uninstalling

```bash
pip uninstall rtk-sf

# Remove the index (optional)
rm -rf .rtk-sf/

# Deregister from Claude Code
claude mcp remove rtk-sf
```

---

## Troubleshooting

### `rtk-sf: command not found`

The pip scripts directory is not on your PATH.

```bash
# Find where pip installed the script
python -m site --user-base
# Add /usr/local/bin or ~/Library/Python/X.Y/bin to PATH

# Or use python -m directly
python -m rtk_sf --version
```

### `watchdog` errors on macOS

```bash
pip install --upgrade watchdog
# On Apple Silicon, try:
pip install watchdog --no-binary watchdog
```

### SQLite FTS5 not available

rtk-sf requires SQLite with FTS5 support (included in Python 3.9+ standard distributions).
If you see FTS5 errors:

```bash
python -c "import sqlite3; conn=sqlite3.connect(':memory:'); conn.execute('CREATE VIRTUAL TABLE t USING fts5(x)'); print('FTS5 OK')"
```

If this fails, your Python distribution may have been built with a minimal SQLite.
Install Python from [python.org](https://python.org) or your system package manager.

### Index shows 0 components

```bash
# Check that .cls files exist
find . -name "*.cls" | head -5

# Try specifying the path explicitly
rtk-sf index --path ./force-app/main/default/classes

# Enable verbose logging
rtk-sf --verbose index
```
