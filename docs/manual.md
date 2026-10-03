# rtk-sf Manual

**Version 0.11.0** · [日本語版](manual.ja.md)

rtk-sf is an MCP server that gives Claude Code a pre-indexed view of your
Salesforce project. Instead of reading a 1,800-line Apex class to answer one
question, Claude reads a 150-token structural summary.

That matters more than a one-off saving suggests. Claude Code caches the
conversation, and a cached token is still re-read on **every** later turn — so
anything placed in context early costs roughly `1.25 + (remaining turns × 0.1)`
times its own size. Measured on a real 242-request session, that is a **23.5×**
multiplier. Keeping tool output small is the whole design.

---

## 1. Requirements

| | |
|---|---|
| Python | 3.9 or newer |
| Claude Code | any recent version |
| Salesforce CLI (`sf`) | only for `sf_command` and `soql_query` |
| Ollama | only for local delegation (§6) — entirely optional |

---

## 2. Install

rtk-sf is **not on PyPI**. Install from git:

```bash
pip install "rtk-sf[all] @ git+https://github.com/furuCRM-Inc/rtk-sf.git@v0.11.0"
```

`pip install rtk-sf` will fail with a 404.

Smaller installs, if you don't want the optional extras:

```bash
# Salesforce only, no vector search, no OCR
pip install "rtk-sf @ git+https://github.com/furuCRM-Inc/rtk-sf.git@v0.11.0"

# Add vector re-ranking (numpy)
pip install "rtk-sf[vector] @ git+https://github.com/furuCRM-Inc/rtk-sf.git@v0.11.0"
```

Verify:

```bash
python3 -m rtk_sf --version     # 0.11.0
```

### Upgrading

```bash
pip install --upgrade --force-reinstall --no-deps \
  "rtk-sf[all] @ git+https://github.com/furuCRM-Inc/rtk-sf.git@v0.11.0"
```

`--force-reinstall` is required: pip will not re-resolve a git install without
it and reports "Requirement already satisfied". `--no-deps` is safe as long as
the release notes don't say a dependency was added.

Re-index after upgrading — the spec format can change between releases.

---

## 3. Set up a project

Two commands, run from your Salesforce project root:

```bash
cd /path/to/your-salesforce-project

# 1. Build the index (writes .rtk-sf/)
python3 -m rtk_sf index

# 2. Register with Claude Code
claude mcp add rtk-sf -- python3 -m rtk_sf serve
```

Use `python3 -m rtk_sf`, not `npx rtk-sf` — rtk-sf is a Python package.

Check it registered:

```bash
claude mcp list        # rtk-sf should appear
```

Then in a Claude Code session, ask something like *"use search_codebase to find
the CSV import logic"*. If Claude answers with component names and not file
dumps, it is working.

### What gets written

```
your-project/
└── .rtk-sf/
    ├── db.sqlite        # FTS5 search index
    ├── specs/           # one compressed YAML spec per component (generated)
    ├── relations.json   # dependency graph
    ├── registry.json    # mtime tracking for incremental re-index
    └── handover/        # local-delegation state (only if you use §6)
```

Add `.rtk-sf/` to `.gitignore`. It is a derived artifact — anyone can rebuild it
with one command.

`specs/` is **generated and overwritten**. Never hand-edit it; use
`annotate_component` to attach notes that survive re-indexing.

---

## 4. Daily use

You do not call these tools yourself — Claude does. The value of knowing them is
being able to say *"use `get_relations` first"* when Claude reaches for a raw
file read.

### Understanding code

| Tool | Use it for |
|---|---|
| `search_codebase` | Find a component by name or keyword. Start here. |
| `query_compressed_spec` | Read one component's fields, methods, and summary |
| `get_class_skeleton` | Read an Apex class surgically — one method's body in full, the rest collapsed |
| `get_relations` | Blast radius before editing: who calls this, what it calls |
| `list_components` | Inventory by type (all Apex classes, all objects, all flows) |
| `annotate_component` | Record a business rule you discovered, so the next session starts with it |

### Data and schema

| Tool | Use it for |
|---|---|
| `nl_to_soql` | Ask a data question in English or Japanese. Try this **before** writing SOQL by hand |
| `soql_query` | Run SOQL with automatic row truncation |
| `get_object_schema` | An object's field list, for building test data |
| `get_record_types` | RecordType definitions without reading the XML |
| `get_lwc_targets` | Which LWC components are exposed, and where |
| `read_data_file` | Preview a CSV/JSON/JSONL file's shape |

`nl_to_soql` is a deterministic compiler — no model call inside it. When it
returns `{"intent": "UNKNOWN"}`, fall back to `get_object_schema` plus a
hand-written `soql_query`. A `RECORD_UPDATE` result is a **proposal**: it is
never executed as DML without your confirmation.

### Validating and deploying

| Tool | Use it for |
|---|---|
| `validate_apex` | Local dry-run: SOQL/DML in loops, unbalanced braces, leftover debug |
| `validate_soql` | Local dry-run on a query before it hits the org |
| `sf_command` | `deploy`, `validate`, `retrieve`, `run_test`, `describe` — returns a summary, not raw CLI output |

`validate_apex` costs nothing and takes no org round-trip. Run it before
`sf_command(action="deploy")`.

### Other languages

The same skeleton idea, for the rest of an enterprise stack:

| Language | Tools |
|---|---|
| Java | `get_java_skeleton`, `run_java_build` (Maven + Gradle) |
| Kotlin | `get_kotlin_skeleton`, `run_gradle` |
| TypeScript / JS | `get_ts_skeleton`, `run_js_tests` (Jest/Vitest/Playwright) |
| Python | `get_python_skeleton`, `run_python_tests` (pytest) |

The `run_*` tools return a single pass-count line on success and only the
failing cases on failure — a green test suite should not cost 4,000 tokens.

### Documentation and housekeeping

| Tool | Use it for |
|---|---|
| `export_system_documentation` | Generate system docs, sequence diagrams, use cases **to disk** |
| `get_project_timeline` | "What have we been working on?" — rolled-up history |
| `get_roi_stats` | Tokens and dollars saved this session |
| `extract_image_text` | Local OCR for a screenshot (EN + JA) |
| `compact_prompt` | Strip filler from a long bilingual prompt |

`export_system_documentation` writes files rather than returning them. A
sequence diagram in chat costs tokens on every subsequent turn; a file on disk
costs nothing.

---

## 5. Keeping the index fresh

The indexer tracks file mtimes, so re-indexing is incremental and cheap:

```bash
python3 -m rtk_sf index                      # only changed files
python3 -m rtk_sf index --force              # rebuild everything
python3 -m rtk_sf index --path force-app/x   # index a subdirectory only
```

For continuous updates while you work:

```bash
python3 -m rtk_sf watch                      # --path also accepted
```

Re-index after `sf project retrieve`, after pulling a branch, and after
upgrading rtk-sf.

If search returns nothing, the index is usually just stale or missing — run
`python3 -m rtk_sf index` first.

---

## 5b. Command reference

| Command | What it does |
|---|---|
| `index` | Build or refresh the index. `--force`, `--path DIR` |
| `watch` | Re-index continuously on file change. `--path DIR` |
| `serve` | Start the MCP stdio server (this is what Claude Code runs) |
| `ui` | Generate an architecture map. `--output FILE` (default `dist/architecture_map.html`) |
| `docs` | Write system documentation to disk. Takes a doc type or `all`, `--output-dir DIR` |
| `timeline` | Print project history as JSON. Takes a scope such as `last_7_days` |
| `hook-stats` | Show recent OCR/NLP hook activity. `-n N` |
| `install` | One-shot setup: index, patch `CLAUDE.md`, wire hooks — **see caution** |
| `update` | Upgrade rtk-sf, then re-run `install` — **see caution** |

A global `--project-root DIR` works on every command, so you can drive a project
from outside it.

> **What `install` does to `CLAUDE.md`.** It refreshes the rtk-sf guidance
> block, delimited by `<!-- rtk-sf:begin <version> -->`. Since 0.11.0 that is
> safe to re-run: the tool table is generated from the tools the installed
> package actually serves, the block is replaced in place so your own sections
> keep their position, the previous file is saved as `CLAUDE.md.rtk-bak`, and a
> block written by a *newer* rtk-sf is left alone rather than downgraded.
> Anything you wrote **inside** the block is still replaced, so keep your own
> notes in their own section.
>
> Hooks in `.claude/settings.json` use a portable `python3` when that
> interpreter can import `rtk_sf`, and an absolute path only when it cannot —
> so a committed `settings.json` stays usable across a team.
>
> The two-step setup in §3 remains the smaller-footprint option if you would
> rather manage `CLAUDE.md` entirely yourself.

---

## 6. Local delegation (optional, new in 0.11.0)

Routes Apex work method-by-method between a local code model and Claude.
Boilerplate edits run locally; anything with real blast radius stays with
Claude. **Off by default** — rtk-sf works fully offline with nothing listening.

### Setup

```bash
# Install a code model
ollama pull qwen2.5-coder:7b

# Optional: non-default host, or a finetune
export RTK_SF_WORKER_URL=http://mac-mini.local:11434
export RTK_SF_WORKER_MODEL=qwen2.5-coder-7b-nexusmesh
```

Pin `RTK_SF_WORKER_MODEL` for a finetune. Auto-detection recognises known
coder-model names (`qwen2.5-coder`, `deepseek-coder`, `codellama`, …), so a
custom tag matches nothing and is rejected as "no code-tuned model" even though
it is the right worker.

### The three tools

| Tool | What it does |
|---|---|
| `hybrid_plan` | Scores every method and shows which route it takes. Call this first |
| `hybrid_delegate` | Runs a batch of method-scoped edits on the local model |
| `hybrid_review` | Records a review finding, or reads the handover delta |

A plan looks like this:

```
plan CsvImportController (CsvImportController.cls, 7 methods)
worker: local:qwen2.5-coder:7b
local tiers: LOW only

-> LOCAL WORKER (1):
  computeHeaderMatchScore [LOW] score=5 loc=9
-> CLAUDE (6):
  importChunk [HIGH] score=24 loc=36 blockers=partial-dml flags=remote-entry
  RowResult [HIGH] score=3 loc=5 NOT-DELEGATABLE:constructor (no return type)
  ...
```

### What stays with Claude, always

Scoring is derived from the source, never from a label in a file — a label
drifts the moment the code changes. These **blockers** force a method to Claude
regardless of how short it is:

SOQL or DML inside a loop · `Savepoint` / `rollback` · partial DML
(`Database.insert(...)`) · HTTP callouts · `without sharing` · FLS/CRUD checks ·
batch Apex · `Messaging.send`

Constructors are excluded entirely, and `@AuraEnabled` raises a method's score
without blocking it.

`MEDIUM`-tier methods also stay with Claude unless you pass `allow_medium`.

### Why you can trust the result without reading it

Nothing reaches a `.cls` file until all of this holds:

1. the output is not truncated;
2. it parses as exactly one method, with the name that was requested;
3. braces, parentheses and string literals balance;
4. **every other method body in the file is byte-identical** — none added, none
   removed, none modified;
5. no ERROR-level `validate_apex` rule fires.

Check 4 is the one that earns the skip: it catches a model that rewrote the
whole class and dropped three methods. The previous file is kept as
`<name>.cls.rtk-bak` (once per batch, so you can always get back to the
pre-batch state) and the write itself is atomic.

**This is a structural guarantee, not a semantic one.** It proves the edit
touched one method and left the file intact. It cannot tell you the logic is
right — run the class's tests for that.

### Self-correction

When output is rejected, the gate's verdict is stored as a correction and fed
back to the model on the next attempt, at zero Claude cost. Use `hybrid_review`
for the semantic problems the gate cannot see:

```
hybrid_review(component_name="AccountService",
              method="validateBillingAddress",
              critique="acc may be null; dereferencing acc.BillingPostalCode
                        throws. Add a null guard first.")
```

The last two critiques are kept verbatim with the failing method body. Once the
method passes, they are distilled into a short, code-free project rule that
applies to **every** method from then on — so a lesson learned on one method is
not lost when that method is fixed.

### Scope

Apex only. Low-code and no-code metadata (Flows, validation rules, field
definitions) are **not** supported: in testing, a validation-rule edit silently
rewrote an unrelated `<errorMessage>`, and the XML gate that caught it is not
production-ready.

---

## 7. Troubleshooting

**`rtk-sf: command not found`**
Use `python3 -m rtk_sf ...` instead. The console script may not be on your PATH.

**Search returns nothing**
Re-index: `python3 -m rtk_sf index`. If it reports 0 components, confirm you are
in the project root and that `.cls` files exist under it.

**MCP server doesn't appear in Claude Code**
```bash
claude mcp list
claude mcp remove rtk-sf
claude mcp add rtk-sf -- python3 -m rtk_sf serve
```
Use an absolute Python path if you installed into a virtualenv.

**Server exits immediately**
Run it in a terminal to see the error: `python3 -m rtk_sf serve`. It logs to
stderr and waits on stdin, so "no output" is normal when started by hand.

**`hybrid_*` says `worker: none`**
Expected when no model is running — everything routes to Claude and no files are
written. If you have one running, check `RTK_SF_WORKER_URL`, and pin
`RTK_SF_WORKER_MODEL` if the tag is a finetune. A failed probe is cached for 30
seconds, so start the daemon and retry shortly after.

**Annotations disappeared**
They shouldn't — but anything hand-written into `.rtk-sf/specs/` will, because
those files are regenerated. Use `annotate_component`.

---

## 8. What rtk-sf does not do

- **It is not a linter or a compiler.** `validate_apex` is a regex dry-run that
  catches common governor-limit and syntax mistakes. It is not a substitute for
  `sf_command(action="validate")` against an org.
- **It does not judge behaviour.** Every guarantee in §6 is structural. Tests
  are still the only thing that tells you the code is correct.
- **It does not call any model by itself.** The only outbound call rtk-sf can
  make is to the local worker in §6, and only when you enable it.
- **It does not execute DML without confirmation.** A `nl_to_soql`
  `RECORD_UPDATE` is a proposal.

---

## See also

- [`installation.md`](installation.md) — per-OS install notes (macOS, Linux, Windows/WSL2)
- [`mcp-integration.md`](mcp-integration.md) — MCP protocol details, Cline setup, JSON-RPC debugging
- [`hybrid-orchestration.md`](hybrid-orchestration.md) — design rationale for §6
- [`documentation-engine.md`](documentation-engine.md) — `export_system_documentation` in depth
- [`roi.md`](roi.md) — the token-saving model
- [`CHANGELOG.md`](../CHANGELOG.md) — what changed in each release
