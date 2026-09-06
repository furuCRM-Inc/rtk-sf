# rtk-sf

**Zero-Token Knowledge & Visual Live-Mapping Layer for Salesforce AI Agents**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/Python-3.9%2B-blue)](https://python.org)
[![MCP Compatible](https://img.shields.io/badge/MCP-Compatible-green)](https://modelcontextprotocol.io)
[![furuCRM](https://img.shields.io/badge/by-furuCRM%20Inc.-0066cc)](https://furucrm.com)

> **Stop wasting tokens on raw file reads. Give your AI agent a pre-indexed knowledge layer instead.**

rtk-sf indexes your entire Salesforce DX project — Apex classes, custom objects, fields, and Flows — into compressed YAML specs served via MCP. Claude Code, Cline, and other MCP-compatible agents can query exact component knowledge in **~300 tokens** instead of reading the full source file (~4,000 tokens). **That's a 92% reduction per lookup.**

---

## The ROI Case

| Scenario | Without rtk-sf | With rtk-sf | Savings |
|---|---|---|---|
| Single Apex class lookup | ~4,000 tokens | ~300 tokens | **92%** |
| Full session (80 classes) | ~320,000 tokens | ~24,000 tokens | **296,000 tokens** |
| Cost per session (Claude Sonnet @ $3/1M) | $0.96 | $0.072 | **$0.888 saved** |
| 20 sessions/month | $19.20/month | $1.44/month | **$17.76/month** |
| 10-developer team | $192/month | $14.40/month | **$177.60/month** |
| **Annual savings (10 devs)** | — | — | **$2,131/year** |

> Full benchmark methodology and enterprise-scale projections: [docs/roi.md](docs/roi.md)

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     Salesforce DX Project                    │
│  force-app/main/default/                                     │
│    classes/AccountService.cls       ← raw: ~4,000 tokens    │
│    objects/Account__c.object-meta.xml                        │
│    flows/OnboardingFlow.flow-meta.xml                        │
└──────────────────┬──────────────────────────────────────────┘
                   │  rtk-sf index
                   ▼
┌─────────────────────────────────────────────────────────────┐
│                      .rtk-sf/                                │
│  registry.json   ← mtime differential tracker               │
│  db.sqlite       ← SQLite FTS5 full-text search             │
│  relations.json  ← nodes + edges graph                      │
│  specs/                                                      │
│    AccountService.yaml  ← compressed: ~300 tokens           │
│    Account__c.yaml                                           │
│    OnboardingFlow.yaml                                       │
└──────────────────┬──────────────────────────────────────────┘
                   │  MCP stdio JSON-RPC
                   ▼
┌─────────────────────────────────────────────────────────────┐
│                   AI Agent (Claude Code / Cline)             │
│                                                              │
│  query_compressed_spec("AccountService")  → 300-token YAML  │
│  search_codebase("payment processing")   → top 5 matches    │
│  get_relations("AccountService")         → callers + deps   │
│  list_components(type="ApexClass")       → all Apex classes  │
└─────────────────────────────────────────────────────────────┘
                   │  optional
                   ▼
┌─────────────────────────────────────────────────────────────┐
│            dist/architecture_map.html  (Cytoscape.js SPA)   │
│                                                              │
│  ● Interactive graph of all components                       │
│  ● Click node → YAML spec in sidebar                        │
│  ● Path highlighting: upstream (amber) / downstream (red)   │
│  ● Full-text search filter                                   │
│  ● Self-contained HTML — no web server needed               │
└─────────────────────────────────────────────────────────────┘
```

---

## Quick Start

**Step 1 — Install**

```bash
pip install rtk-sf
```

**Step 2 — Index your Salesforce project**

```bash
cd your-salesforce-project
rtk-sf index
```

```
rtk-sf indexer starting...
  Project root : /projects/my-org
  Source path  : /projects/my-org/force-app
Indexing complete:
  Indexed : 84
  Skipped : 0 (unchanged)
  Errors  : 0
Synced 84 components into search index.
```

**Step 3 — Register with Claude Code**

```bash
claude mcp add rtk-sf -- python -m rtk_sf serve
```

Your AI agent now has instant, token-efficient access to your entire Salesforce codebase.

---

## MCP Integration

### Claude Code

```bash
# Register the MCP server (run once per project)
claude mcp add rtk-sf -- python -m rtk_sf serve

# Verify
claude mcp list
```

Once registered, Claude Code can call these tools directly:

```
Claude: I need to understand AccountService.
→ [calls query_compressed_spec("AccountService")]
→ Returns 300-token YAML instead of reading the 4,000-token .cls file

Claude: Find all payment-related code.
→ [calls search_codebase("payment processing", limit=5)]
→ Returns ranked list of matching components with snippets

Claude: What calls AccountService?
→ [calls get_relations("AccountService")]
→ Upstream: [OrderTriggerHandler, QuoteController]
   Downstream: [PaymentGateway, EmailService]
```

### Cline

Add to your Cline MCP configuration (`.cline/mcp.json` or VS Code settings):

```json
{
  "mcpServers": {
    "rtk-sf": {
      "command": "python",
      "args": ["-m", "rtk_sf", "serve"],
      "cwd": "/path/to/your/salesforce/project"
    }
  }
}
```

### Any MCP-compatible client

```bash
# Start the server manually
python -m rtk_sf serve

# The server reads JSON-RPC from stdin, writes to stdout
# Protocol: MCP 2024-11-05
```

---

## Visual Architecture Map

Generate an interactive HTML graph of your entire component landscape:

```bash
rtk-sf ui
# Opens: dist/architecture_map.html
open dist/architecture_map.html
```

**Features:**
- Interactive graph powered by Cytoscape.js (CoSE layout)
- Click any node to view its compressed YAML spec in the sidebar
- Path highlighting: selected (amber), upstream callers (light amber), downstream deps (red)
- Search box to filter/dim non-matching nodes
- Re-layout button for large graphs
- Keyboard shortcuts: `Esc` clear, `Ctrl+K` / `F` focus search
- Self-contained single HTML file — share with your team, open in any browser
- Dark theme with furuCRM branding

> **Screenshot:** [docs/architecture_map_demo.png](docs/architecture_map_demo.png)

---

## How It Works

### Differential Indexing

rtk-sf tracks file modification times in `.rtk-sf/registry.json`. On subsequent `rtk-sf index` runs, only changed files are re-parsed — making incremental indexing fast even on large orgs.

```
First run  (84 files): ~2.3 seconds
Re-index (3 changed) : ~0.1 seconds
```

### Hybrid Search

Keyword search uses SQLite's built-in **FTS5** full-text search — no external dependencies, no network calls. When `numpy` is installed (`pip install rtk-sf[vector]`), results are re-ranked using bag-of-words cosine similarity for improved relevance.

### YAML Compression

Instead of the full Apex source, rtk-sf extracts only what the AI agent needs to reason about a component:

```yaml
# Full Apex class: ~4,000 tokens
# rtk-sf spec: ~300 tokens (92% reduction)

component: AccountService
type: ApexClass
summary: Handles Account CRUD operations and related business logic
methods:
  - name: createAccount
    returns: Account
    params: [String name, String industry]
    description: Creates and inserts a new Account record
  - name: getAccountsByIndustry
    returns: List<Account>
    params: [String industry]
    description: Returns all Accounts matching the given industry
  - name: updateBillingAddress
    returns: void
    params: [Id accountId, Address newAddress]
```

### Live Watch Mode

```bash
rtk-sf watch
# Watching: force-app/
# Ctrl+C to stop
```

Automatically re-indexes any `.cls` or `.xml` file that changes on disk. Ideal for active development sessions.

---

## All Commands

```
rtk-sf index               # Index ./force-app (differential)
rtk-sf index --path ./src  # Custom source directory
rtk-sf index --force       # Force re-index all files

rtk-sf watch               # Live file watcher
rtk-sf watch --path ./src  # Watch custom directory

rtk-sf serve               # Start MCP stdio server

rtk-sf ui                  # Generate dist/architecture_map.html
rtk-sf ui --output ~/map.html  # Custom output path

rtk-sf --version           # Show version
rtk-sf --help              # Show help
```

---

## MCP Tools Reference

| Tool | Parameters | Returns |
|---|---|---|
| `query_compressed_spec` | `component_name: str` | YAML spec (~300 tokens) |
| `search_codebase` | `query: str`, `limit: int = 5` | Ranked results with snippets |
| `get_relations` | `component_name: str` | Upstream callers + downstream deps |
| `list_components` | `type: str = "all"` | All indexed components by type |

---

## Supported Salesforce Metadata

| Type | Source | What is indexed |
|---|---|---|
| Apex Class | `*.cls` | Class name, ApexDoc summary, method signatures + descriptions |
| Custom Object | `*.object-meta.xml` | Label, fields list, lookup relationships |
| Custom Field | `*/fields/*.field-meta.xml` | Name, type, label, required, description |
| Flow | `*.flow-meta.xml` | Label, process type, status, element counts |

More types coming: Permission Sets, Custom Labels, Triggers, LWC.

---

## Installation

### From PyPI

```bash
pip install rtk-sf
```

### With vector re-ranking

```bash
pip install "rtk-sf[vector]"
```

### From source

```bash
git clone https://github.com/furuCRM/rtk-sf.git
cd rtk-sf
pip install -e ".[dev]"
```

### Requirements

- Python 3.9+
- Salesforce DX project with `force-app/` structure
- `watchdog` (for watch mode)
- `pyyaml` (included)
- `numpy` (optional, for vector re-ranking)

---

## Project Structure

```
rtk-sf/
├── rtk_sf/
│   ├── __init__.py        # Package exports
│   ├── __main__.py        # CLI entry point
│   ├── indexer.py         # Differential parser (Apex, XML, objects)
│   ├── search.py          # SQLite FTS5 + vector hybrid search
│   ├── watcher.py         # OS file watcher (watchdog)
│   ├── mcp_server.py      # MCP stdio JSON-RPC server
│   └── ui_generator.py    # Generates dist/architecture_map.html
├── ui/
│   └── template.html      # Cytoscape.js SPA template reference
├── docs/
│   ├── installation.md    # Platform-specific install guide
│   ├── mcp-integration.md # MCP setup for Claude Code & Cline
│   └── roi.md             # Detailed ROI analysis
└── scripts/
    └── install.sh         # One-command setup script
```

---

## Contributing

Contributions are welcome. Please read [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

Quick start for contributors:

```bash
git clone https://github.com/furuCRM/rtk-sf.git
cd rtk-sf
pip install -e ".[dev]"
pytest
```

---

## Roadmap

- [ ] Permission Set indexing
- [ ] Custom Label indexing
- [ ] Apex Trigger indexing (separate from class)
- [ ] LWC component indexing (HTML + JS summary)
- [ ] VS Code extension with inline spec preview
- [ ] GitHub Actions integration for CI spec validation
- [ ] Org-aware indexing (pull metadata from connected org via `sf` CLI)

---

## License

[MIT](LICENSE) — free to use, modify, and distribute.

---

Built with love by [furuCRM Inc.](https://furucrm.com)

*Helping Salesforce development teams move faster with AI.*
