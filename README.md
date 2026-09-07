# rtk-sf

**Zero-Token Knowledge & Visual Live-Mapping Layer for Salesforce AI Agents**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Version](https://img.shields.io/badge/version-0.4.1-brightgreen)](https://github.com/furuCRM-Inc/rtk-sf/releases)
[![Python 3.9+](https://img.shields.io/badge/Python-3.9%2B-blue)](https://python.org)
[![MCP Compatible](https://img.shields.io/badge/MCP-Compatible-green)](https://modelcontextprotocol.io)
[![furuCRM](https://img.shields.io/badge/by-furuCRM%20Inc.-0066cc)](https://www.furucrm.com)

> **Stop wasting tokens on raw file reads. Give your AI agent a pre-indexed knowledge layer instead.**

rtk-sf indexes your entire Salesforce DX project — Apex classes, custom objects, fields, and Flows — into compressed YAML specs served via MCP. Claude Code can query exact component knowledge in **~300 tokens** instead of reading the full source file (~4,000 tokens). **That's a 92% reduction per lookup.**

---

## Before vs. After

```
❌  WITHOUT rtk-sf                      ✅  WITH rtk-sf
─────────────────────────────────────   ─────────────────────────────────────
Claude: "Show me AccountService"        Claude: "Show me AccountService"
  → reads AccountService.cls            → calls query_compressed_spec()
  → reads AccountService.cls-meta.xml  → returns YAML spec instantly
  → reads related trigger files
  → reads test class for context
                                        Tokens consumed:  ~300
Tokens consumed:  ~15,000               Time:             <0.1 s
Time:             ~8 s                  Cost (@$3/1M):    $0.0009
Cost (@$3/1M):    $0.045
                                        Savings per lookup: 98%
```

---

## ROI Calculator

> **Plug in your team size — the numbers speak for themselves.**

| Team size | Sessions/month | Without rtk-sf | With rtk-sf | **Monthly savings** |
|---|---|---|---|---|
| Solo dev | 20 | $19.20 | $0.58 | **$18.62** |
| 3-dev team | 60 | $57.60 | $1.73 | **$55.87** |
| 5-dev team | 100 | $96.00 | $2.88 | **$93.12** |
| 10-dev team | 200 | $192.00 | $5.76 | **$186.24** |
| 20-dev team | 400 | $384.00 | $11.52 | **$372.48** |
| **20 devs, annual** | — | **$4,608/yr** | **$138/yr** | **🔥 $4,470/yr saved** |

*Assumptions: Claude Sonnet 4 @ $3/1M input tokens · 80 component lookups per session · 15,000 tokens without rtk-sf vs. 450 tokens with.*

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
│                        AI Agent (Claude Code)                │
│                                                              │
│  query_compressed_spec("AccountService")  → 300-token YAML  │
│                                             + annotations    │
│  search_codebase("payment processing")   → top 5 matches    │
│  search_codebase("承認フロー")             → Japanese OK      │
│  get_relations("AccountService")         → callers + deps   │
│  list_components(type="ApexClass")       → all Apex classes  │
│  annotate_component("Account__c", ...)   → write knowledge  │
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

### Option A — One-liner (recommended)

```bash
curl -sSL https://raw.githubusercontent.com/furuCRM-Inc/rtk-sf/main/scripts/install.sh | bash
```

This checks Python, installs rtk-sf, indexes your project, and prints your next steps — all in one command.

### Option B — Manual

**Step 1 — Install**

```bash
pip install git+https://github.com/furuCRM-Inc/rtk-sf.git@main
# With vector re-ranking (optional):
pip install "git+https://github.com/furuCRM-Inc/rtk-sf.git@main[vector]"
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

**Step 4 — Tell Claude to use rtk-sf (critical)**

The install script does this automatically. If you ran it manually, add this block to the top of your `CLAUDE.md`:

```markdown
## Code Search & Data — Use rtk-sf First (Required)

| Task | Tool to call |
|---|---|
| Find a component by name or keyword | `search_codebase(query)` |
| Read a component spec / fields / methods | `query_compressed_spec(component_name)` |
| Blast-radius before editing | `get_relations(component_name)` |
| List all Apex classes / objects / flows | `list_components(type)` |
| Write discovered business logic back | `annotate_component(component_name, key, value)` |
| Read an Apex class before editing (surgical) | `get_class_skeleton(component_name, focus_methods)` |
| Deploy / retrieve / run tests silently | `sf_command(action, target_org, ...)` |
| Get object field list for data creation | `get_object_schema(object_name)` |
| Inspect existing records (sample only) | `soql_query(query, target_org, sample_size)` |

Never open raw `.cls` or `.xml` files unless the spec is insufficient.
```

Without this, Claude defaults to reading raw source files and ignores the MCP tools.

**Step 5 — (Optional) Generate the visual architecture map**

```bash
rtk-sf ui && open dist/architecture_map.html
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

### Any MCP-compatible client

```bash
# Start the server manually
python -m rtk_sf serve

# The server reads JSON-RPC 2.0 from stdin, writes to stdout
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

Keyword search uses SQLite's built-in **FTS5** full-text search — no external dependencies, no network calls. When `numpy` is installed (`pip install "git+https://github.com/furuCRM-Inc/rtk-sf.git@main[vector]"`), results are re-ranked using bag-of-words cosine similarity for improved relevance.

**Japanese search is fully supported.** rtk-sf uses the FTS5 `trigram` tokenizer combined with a LIKE fallback for 1–2 character terms, so Japanese metadata labels, picklist values, and annotation text are all searchable:

```bash
# All of these work — including short Japanese terms
search_codebase("承認")    # 2-char: LIKE fallback → hits Approval__c, ApprovalFlow fields
search_codebase("取引")    # 2-char: LIKE fallback → hits Account, OrderService, related fields
search_codebase("承認フロー") # 4-char: FTS5 trigram  → hits ApprovalFlow, ApprovalStage__c
search_codebase("顧客管理")  # 4-char: FTS5 trigram  → hits AccountService, CustomerService
```

**CamelCase splitting** is also applied at index time — `ExamTicketDownloadController` is indexed as both the full identifier and its word fragments (`Exam`, `Ticket`, `Download`, `Controller`), so partial English searches work without knowing the exact component name.

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
| `query_compressed_spec` | `component_name: str` | YAML spec (~300 tokens) + all annotations |
| `search_codebase` | `query: str`, `limit: int = 5` | Ranked results with snippets (English & Japanese) |
| `get_relations` | `component_name: str` | Upstream callers + downstream deps |
| `list_components` | `type: str = "all"` | All indexed components by type |
| `annotate_component` | `component_name`, `key`, `value`, `source` | Saves discovered business logic back to the index |
| `get_class_skeleton` | `component_name: str`, `focus_methods: list` | Apex source with non-focus method bodies collapsed (~280 tokens) |
| `sf_command` | `action: str`, `target_org: str`, `...` | Runs sf CLI silently, returns JSON result |
| `get_object_schema` | `object_name: str` | Compact field profile from local index (~150 tokens, no org call) |
| `soql_query` | `query: str`, `target_org: str`, `sample_size: int = 3` | Capped SOQL result (3 clean rows with truncation notice) |

### `annotate_component` — Knowledge Annotation (v0.3.0)

When your AI agent discovers business logic hidden inside method bodies — conditions, SOQL filters, access rules — it can write that knowledge back to the index so future agents find it without re-reading the source.

```
# First session: AI reads source and discovers a condition
Agent → [reads OrderApprovalController.cls]
      → finds: if (order.Status__c != '承認済') throw AuraHandledException
      → [calls annotate_component(
            component_name = "Order__c",
            key            = "business_rule",
            value          = "Approval update (saveApproval) only allowed when Status__c = '承認済'. Owner check: AssignedUser__r.Contact__c = current user.",
            source         = "ai_discovery"
         )]

# All future sessions: no source read needed
Agent → [calls search_codebase("approval condition")]
      → returns Order__c with annotation in results

Agent → [calls query_compressed_spec("Order__c")]
      → returns YAML spec PLUS:
         ## Annotations (discovered business logic)
         [business_rule] (ai_discovery · 2026-09-06)
           Approval update only allowed when Status__c = '承認済'. ...
```

**Virtuous cycle**: each session makes the knowledge base richer for the next one — at zero additional token cost.

---

## Supported Salesforce Metadata

rtk-sf indexes all major Salesforce metadata types supported by the sf CLI, grouped below by category.

### Code / Programmatic

| Type | Source | What is indexed |
|---|---|---|
| ApexClass | `*.cls` | Class name, ApexDoc summary, all method signatures + descriptions |
| ApexTrigger | `*.trigger` | Trigger name, sObject, trigger events (before/after insert/update/…) |
| ApexPage | `*.page` | Controller, title attribute |
| ApexComponent | `*.component` | Controller, access attribute |
| LightningComponentBundle (LWC) | `lwc/<name>/` directory | Targets, `@api` properties, public methods, child component references |
| AuraDefinitionBundle | `aura/<name>/` directory | Bundle type (Component/App), `<aura:attribute>` declarations |

### UI / Metadata

| Type | Source | What is indexed |
|---|---|---|
| Custom Object | `*.object-meta.xml` | Label, fields list, lookup relationships |
| Custom Field | `*/fields/*.field-meta.xml` | Name, type, label, required, description |
| Flow | `*.flow-meta.xml` | Label, process type, status, element counts |
| FlexiPage | `*.flexipage-meta.xml` | Page type, template, component count + references |
| Layout | `*.layout-meta.xml` | Section count, related list count |
| CompactLayout | `*.compactLayout-meta.xml` | Label, fields list |
| ListView | `*.listView-meta.xml` | Label, filter scope, columns |
| QuickAction | `*.quickAction-meta.xml` | Type, target object, label |
| CustomTab | `*.tab-meta.xml` | Custom object, Aura component, or page reference |

### Security / Access

| Type | Source | What is indexed |
|---|---|---|
| Profile | `*.profile-meta.xml` | User license, object permissions (CRUD), enabled user permissions |
| PermissionSet | `*.permissionset-meta.xml` | Object permissions, enabled user permissions |
| PermissionSetGroup | `*.permissionsetgroup-meta.xml` | Included permission sets list |
| CustomPermission | `*.customPermission-meta.xml` | Label, description |

### Rules / Automation

| Type | Source | What is indexed |
|---|---|---|
| ValidationRule | embedded in `*.object-meta.xml` | Active flag, formula, error message, description |
| WorkflowRule | `*.workflow-meta.xml` | Rule names, trigger types, action counts |
| AssignmentRules | `*.assignmentRules-meta.xml` | Rule count |
| EscalationRules | `*.escalationRules-meta.xml` | Rule count |
| AutoResponseRules | `*.autoResponseRules-meta.xml` | Rule count |
| SharingRules | `*.sharingRules-meta.xml` | Owner rule count, criteria rule count |

### Data / Config

| Type | Source | What is indexed |
|---|---|---|
| CustomMetadata | `*.md-meta.xml` | Label, field/value pairs |
| CustomLabel | `*.labels-meta.xml` | Label count, all fullName/value/language/categories entries |
| GlobalValueSet | `*.globalValueSet-meta.xml` | Master label, all picklist values |
| StandardValueSet | `*.standardValueSet-meta.xml` | All standard values |
| RecordType | `*.recordType-meta.xml` | Full name, label, active, business process |
| MatchingRule | `*.matchingRule-meta.xml` | Active, matching rule item count |
| DuplicateRule | `*.duplicateRule-meta.xml` | Master label, active, matching rules list |

### App / Navigation

| Type | Source | What is indexed |
|---|---|---|
| CustomApplication | `*.app-meta.xml` | Label, nav type, tab count + list |
| AppMenu | `*.appMenu-meta.xml` | App menu item count |
| HomePageLayout | `*.homePageLayout-meta.xml` | Component count + list |

### Integration / External

| Type | Source | What is indexed |
|---|---|---|
| ConnectedApp | `*.connectedApp-meta.xml` | Label, OAuth scopes |
| NamedCredential | `*.namedCredential-meta.xml` | Label, endpoint URL, principal type |
| RemoteSiteSetting | `*.remoteSite-meta.xml` | URL, active flag, description |
| AuthProvider | `*.authprovider-meta.xml` | Provider type, friendly name |
| CspTrustedSite | `*.cspTrustedSite-meta.xml` | Endpoint URL, active flag |

### Email

| Type | Source | What is indexed |
|---|---|---|
| EmailTemplate | `*.email-meta.xml` | Name, subject, type, description |

### Agentforce / AI

| Type | Source | What is indexed |
|---|---|---|
| PromptTemplate | `*.prompttemplate-meta.xml` | Master label, type, template type, active version count |
| GenAiPromptTemplate | `*.genAiPromptTemplate-meta.xml` | Master label, type |
| GenAiFunction | `*.genAiFunction-meta.xml` | Master label, description, function definition |
| AIApplication | `*.aiApplication-meta.xml` | Developer name, status |
| Bot / BotVersion | `*.bot-meta.xml`, `*.botVersion-meta.xml` | Label, private conversation log setting, dialog count |

### Analytics

| Type | Source | What is indexed |
|---|---|---|
| WaveApplication | `*.wapp-meta.xml` | Name, label |
| WaveDashboard | `*.wdash-meta.xml` | Name, label |

### Static / Assets

| Type | Source | What is indexed |
|---|---|---|
| StaticResource | `*.resource-meta.xml` | Content type, cache control, description |
| ContentAsset | `*.asset-meta.xml` | Master label, language |

---

## Installation

### From GitHub

```bash
pip install git+https://github.com/furuCRM-Inc/rtk-sf.git@main
```

### With vector re-ranking

```bash
pip install "git+https://github.com/furuCRM-Inc/rtk-sf.git@main[vector]"
```

### From source

```bash
git clone https://github.com/furuCRM-Inc/rtk-sf.git
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
│   ├── mcp_server.py      # MCP stdio JSON-RPC server (9 tools)
│   ├── skeleton.py        # Apex class skeleton slicer (v0.4.0)
│   ├── sf_runner.py       # Silent sf CLI wrapper (v0.4.0)
│   ├── data_tools.py      # Mock schema + SOQL truncation (v0.4.1)
│   └── ui_generator.py    # Generates dist/architecture_map.html
├── ui/
│   └── template.html      # Cytoscape.js SPA template reference
├── docs/
│   ├── installation.md    # Platform-specific install guide
│   ├── mcp-integration.md # MCP setup for Claude Code
│   └── roi.md             # Detailed ROI analysis
└── scripts/
    └── install.sh         # One-command setup script
```

---

## Contributing

We actively want the Salesforce developer community to build on top of rtk-sf. Here are the most impactful ways to contribute right now:

### 🔧 High-Impact: Write a new metadata parser

The indexer lives in `rtk_sf/indexer.py`. Adding a new parser means AI agents can understand one more Salesforce metadata type without reading raw XML. Open tasks:

| Metadata | File pattern | Status |
|---|---|---|
| OmniStudio FlexCard | `*.flexCard-meta.xml` | **wanted** |
| OmniStudio DataRaptor | `*.dataRaptor-meta.xml` | **wanted** |
| Experience Cloud page | `*.json` (ExperienceBundle) | **wanted** |
| Slack App | `*.slackApp-meta.xml` | **wanted** |
| Custom Notification | `*.customNotificationType-meta.xml` | **wanted** |

See [CONTRIBUTING.md](CONTRIBUTING.md) for the 30-line parser template.

### 📊 Medium: Improve the architecture map

`rtk_sf/ui_generator.py` generates the Cytoscape.js SPA. Ideas:

- Add edge labels showing the relationship type (calls / references / extends)
- Add a timeline view sorted by `updated_at` (shows recently changed components)
- Export the graph as PNG/SVG

### 📝 Easy: Add annotations from your own project

If you discover business rules, access conditions, or SOQL filters that are important to document, use `annotate_component` and open a discussion — we want to build a community knowledge base.

### Quick start for contributors

```bash
git clone https://github.com/furuCRM-Inc/rtk-sf.git
cd rtk-sf
pip install -e ".[dev]"
pytest
```

---

## Roadmap

- [x] Permission Set indexing
- [x] Custom Label indexing
- [x] Apex Trigger indexing (separate from class)
- [x] LWC component indexing (HTML + JS summary)
- [x] Aura bundle indexing
- [x] Full coverage of all sf CLI metadata types (v0.2.0)
- [x] Japanese search — FTS5 trigram + LIKE fallback for 1–2 char terms (v0.3.0)
- [x] CamelCase splitting for partial English identifier search (v0.3.0)
- [x] `annotate_component` MCP tool — write discovered business logic back to index (v0.3.0)
- [x] Annotations included in `query_compressed_spec` response (v0.3.0)
- [x] `get_class_skeleton` — surgical Apex read, collapses non-focus method bodies (v0.4.0)
- [x] `sf_command` — silent sf CLI wrapper with JSON output (v0.4.0)
- [x] `get_object_schema` — compact field profile from local index, zero org calls (v0.4.1)
- [x] `soql_query` — SOQL with enforced row cap and truncation notice (v0.4.1)
- [ ] VS Code extension with inline spec preview
- [ ] GitHub Actions integration for CI spec validation
- [ ] Org-aware indexing (pull metadata from connected org via `sf` CLI)
- [ ] Annotation export/import for team knowledge sharing

---

## License

[MIT](LICENSE) — free to use, modify, and distribute.

---

Built with love by [furuCRM Inc.](https://www.furucrm.com)

*Helping Salesforce development teams move faster with AI.*
