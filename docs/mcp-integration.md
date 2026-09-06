# MCP Integration Guide

rtk-sf implements the **Model Context Protocol (MCP) 2024-11-05** over stdio,
making it compatible with Claude Code, Cline, and any other MCP-compatible AI agent.

---

## Quick Setup (Claude Code)

### Step 1: Index your project

```bash
cd /path/to/your/salesforce-project
rtk-sf index
```

### Step 2: Register the MCP server

```bash
claude mcp add rtk-sf -- python -m rtk_sf serve
```

This tells Claude Code to launch `python -m rtk_sf serve` as an MCP subprocess
whenever a session starts. The server reads from stdin and writes to stdout
(MCP stdio transport).

### Step 3: Verify

```bash
claude mcp list
```

Expected output:
```
rtk-sf: python -m rtk_sf serve  (local)
```

### Step 4: Test in a Claude Code session

Start a new Claude Code session in your project directory and try:

```
Show me the spec for AccountService.
```

Claude Code will call `query_compressed_spec("AccountService")` and return
the compressed YAML (~300 tokens) instead of reading the raw 4,000-token file.

---

## MCP Tools Reference

### `query_compressed_spec`

Return the compressed YAML specification for a named Salesforce component.

```json
{
  "name": "query_compressed_spec",
  "arguments": {
    "component_name": "AccountService"
  }
}
```

**Response:**
```yaml
component: AccountService
type: ApexClass
summary: Handles Account CRUD operations and business logic
methods:
  - name: createAccount
    returns: Account
    params: [String name, String industry]
    description: Creates and inserts a new Account record
  - name: getAccountsByIndustry
    returns: List<Account>
    params: [String industry]
```

Token cost: ~300 tokens (vs ~4,000 for raw file read — **92% reduction**)

---

### `search_codebase`

Full-text search across all indexed components.

```json
{
  "name": "search_codebase",
  "arguments": {
    "query": "payment processing",
    "limit": 5
  }
}
```

**Response:**
```
Search results for 'payment processing':

1. [ApexClass] PaymentService
   ...Handles payment gateway integration and transaction processing...

2. [ApexClass] OrderController
   ...validatePayment, processOrder, payment processing workflow...

3. [Flow] PaymentApprovalFlow
   ...processType: AutoLaunchedFlow, status: Active...
```

---

### `get_relations`

Upstream callers and downstream dependencies for a component.

```json
{
  "name": "get_relations",
  "arguments": {
    "component_name": "AccountService"
  }
}
```

**Response:**
```
Relations for: AccountService

Upstream callers (3):
  - AccountTriggerHandler
  - QuoteController
  - B2BOrderService

Downstream dependencies (2):
  - EmailService
  - PaymentGateway
```

Use this before making changes to understand blast radius.

---

### `list_components`

List all indexed components, optionally filtered by type.

```json
{
  "name": "list_components",
  "arguments": {
    "type": "ApexClass"
  }
}
```

**Supported types:** `all`, `ApexClass`, `CustomObject`, `CustomField`, `Flow`

**Response:**
```
Indexed components (84 total):

ApexClass (42):
  - AccountService
  - B2BOrderService
  - EmailService
  ...

CustomObject (18):
  - Account__c
  - Opportunity__c
  ...
```

---

## Example Claude Code Session

Here is a realistic session showing rtk-sf in action:

```
User: I need to add a new method to AccountService that sends a welcome email.
      What does AccountService currently look like?

Claude Code:
  [calls query_compressed_spec("AccountService")]
  → 300 tokens consumed

  AccountService is an Apex class that handles Account CRUD operations.
  It has 8 methods: createAccount, updateAccount, deleteAccount,
  getAccountsByIndustry, getAccountById, validateAccount,
  sendNotification, archiveAccount.

  The sendNotification method already exists and calls EmailService.
  I'll add sendWelcomeEmail similarly...

User: What components call AccountService?

Claude Code:
  [calls get_relations("AccountService")]
  → 50 tokens consumed

  AccountService is called by:
  - AccountTriggerHandler (after insert/update)
  - QuoteController (on quote approval)
  - B2BOrderService (on order creation)

  I'll need to verify that adding sendWelcomeEmail doesn't break
  any of these callers...
```

**Without rtk-sf:** Claude Code would read the raw AccountService.cls file
(~4,000 tokens) plus potentially related class files. A typical session
exploring 10 related classes costs ~40,000 tokens.

**With rtk-sf:** The same exploration costs ~3,000–5,000 tokens. **87–92% reduction.**

---

## Cline Setup

### VS Code settings.json

```json
{
  "cline.mcpServers": {
    "rtk-sf": {
      "command": "python",
      "args": ["-m", "rtk_sf", "serve"],
      "cwd": "${workspaceFolder}",
      "env": {}
    }
  }
}
```

### .cline/mcp.json (project-level)

```json
{
  "mcpServers": {
    "rtk-sf": {
      "command": "python",
      "args": ["-m", "rtk_sf", "serve"]
    }
  }
}
```

---

## Other MCP Clients

rtk-sf speaks standard MCP 2024-11-05 over stdio. Any client supporting this
transport can use it.

### Manual JSON-RPC (debugging)

```bash
# Start the server in a terminal
python -m rtk_sf serve

# In another terminal, send requests via pipe
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}' | python -m rtk_sf serve

# List tools
echo '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' | python -m rtk_sf serve

# Query a spec
echo '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"query_compressed_spec","arguments":{"component_name":"AccountService"}}}' | python -m rtk_sf serve
```

---

## Advanced: Per-project MCP Config

If you work on multiple Salesforce projects, configure rtk-sf per-project
with `--project-root`:

```bash
# Project A
claude mcp add rtk-sf-projecta -- python -m rtk_sf serve --project-root /projects/org-a

# Project B
claude mcp add rtk-sf-projectb -- python -m rtk_sf serve --project-root /projects/org-b
```

Or use virtual environments per project:

```bash
cd /projects/org-a
python -m venv .venv
.venv/bin/pip install rtk-sf
claude mcp add rtk-sf -- /projects/org-a/.venv/bin/python -m rtk_sf serve
```

---

## Keeping the Index Fresh

The index is not auto-updated when you add new Apex classes or modify
existing ones (unless watch mode is running).

**Option 1: Re-index manually**
```bash
rtk-sf index
```

**Option 2: Live watch mode** (recommended during active development)
```bash
rtk-sf watch
```
Leave this running in a terminal. Any `.cls` or `.xml` file change triggers
an immediate incremental re-index.

**Option 3: Git hook**

Add to `.git/hooks/post-checkout` and `.git/hooks/post-merge`:

```bash
#!/bin/bash
rtk-sf index
```

```bash
chmod +x .git/hooks/post-checkout .git/hooks/post-merge
```

---

## Troubleshooting

### "Component not found in index"

```bash
# Re-run the indexer
rtk-sf index --force

# Check if the component was indexed
rtk-sf --verbose index 2>&1 | grep "AccountService"
```

### MCP server not appearing in Claude Code

```bash
# Check registration
claude mcp list

# Remove and re-add
claude mcp remove rtk-sf
claude mcp add rtk-sf -- python -m rtk_sf serve

# Verify python path
which python
python -m rtk_sf --version
```

### Server crashes immediately

```bash
# Run the server manually to see errors
python -m rtk_sf --verbose serve

# Check for .rtk-sf/ directory
ls .rtk-sf/
```
