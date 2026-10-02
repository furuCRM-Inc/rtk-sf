# Changelog

All notable changes to rtk-sf are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Planned
- Permission Set indexing
- Custom Label indexing
- LWC component indexing (HTML + JS summary)
- Apex Trigger indexing (separate from class)
- VS Code extension for inline spec preview
- GitHub Actions CI integration

---

## [0.10.3] — 2026-10-02

Fixes [#31](https://github.com/furuCRM-Inc/rtk-sf/issues/31), the follow-up verification
of [#28](https://github.com/furuCRM-Inc/rtk-sf/issues/28). The two items confirmed fixed
in 0.10.2 (`validate` + `class_names`, duplicated skeleton bodies) stay fixed; these are
the three that were still open, plus one defect found while fixing them.

### Fixed

**`sf_command` answered with raw CLI flag errors (request A)**

`describe` runs `sf org display`, which has no `--metadata` and no `--source-dir`, so
passing them came back as a bare `Nonexistent flag: --metadata` — no indication of which
action does take them. Each action now declares the arguments its command actually has:

- a selector the action cannot use (`metadata`, `source_dir`, `class_names`, `test_level`,
  `sobject`) is refused with the actions it belongs to, before anything is executed;
- `wait` is *dropped* rather than refused when the command has no `--wait`
  (`sf org display --wait 10` also fails) — an MCP client fills it from the schema
  default, so failing the call would be worse than ignoring a number nobody set.

**`describe` could not describe an object (request A)**

The action name reads both ways, so `sobject` is now accepted: `describe` with `sobject`
routes to the new `describe_object` action (`sf sobject describe`), which reports the
object's shape — label, custom/standard, field counts, record types, child relationships,
key prefix, permissions — and points at `get_object_schema` for the field list rather than
dumping tens of thousands of tokens of describe payload.

**`cwd` was accepted and ignored**

`sf` resolves source paths and the default org from the DX project it runs in, so a `cwd`
in the arguments is now the subprocess's working directory. A non-directory is reported
before the command runs.

**Japanese search found the wrong components (request B)**

`search_codebase("セルフ登録 職員番号 生年月日 有資格者リスト")` returned
`Application__c.BirthDate__c` and `Application__c.StaffNumber__c` — two leaf fields —
while the LWC `selfRegistration` and the Apex `SelfRegistrationController` /
`SelfRegistrationService` that implement the feature did not appear at all, and
`"セルフ登録"` alone returned 0 where `"register"` found all three.

- **Vocabulary.** Nothing connected a Japanese word to an English identifier. A curated
  JA↔EN table (same idea as `soql_compiler.FIELD_SYNONYM_MAP`, no model call) now maps
  both directions, so `セルフ登録` reaches `SelfRegistrationController` and `staff number`
  reaches `職員番号`. Projects extend it in `.rtk-sf/synonyms.yaml`; a malformed file is
  ignored rather than breaking search.
- **Ranking.** Results order by matched terms, then by matches on the component's **own
  name**, then **implementation units over their leaves** (an Apex class or LWC above one
  of its fields), then terms matched as typed over terms reached by synonym. Each result
  reports `matched_terms`, `name_matches` and `direct_matches`.

**A deploy whose tests failed was reported as a success**

Apex test failures leave `numberComponentErrors` at 0 while `status` is `Failed`, which
the headline rendered as `✅ Failed: 2 component(s)`.

### Added

**Apex test results on deploy/validate (request C)**

A validate with `RunSpecifiedTests` reported only `✅ Succeeded: 2 component(s) in 18.9s`
— nothing about whether the tests it was asked to run had run. Deploy and validate now
add test counts, up to three failing test names with their messages, and code-coverage
warnings:

```
❌ Failed: 2 component(s) in 42.0s
❌ Tests: 5/7 passed — 2 failed
  • SelfRegistrationControllerTest.testStaffNumber: System.AssertException: Expected: 1, Actual: 0
  • ApplicationServiceTest.testBirthDate: List has no rows
  ⚠️ coverage: Test coverage of selected Apex Class is 62%, at least 75% is required
```

A deploy that enabled tests but ran none says so instead of staying silent.

### Known issues

Unchanged from 0.10.2: the skill-priority hooks of the third-party
`salesforce-development` plugin can block read-only log/query/anonymous-Apex commands.
That plugin is outside rtk-sf. The `No such column` failures noted in #31 are
field-level security in the org, not an rtk-sf defect.

---

## [0.10.2] — 2026-10-02

Fixes [#28](https://github.com/furuCRM-Inc/rtk-sf/issues/28).

### Fixed

**Japanese search returned 0 results where an English word worked**

`search_codebase("セルフ登録 職員番号")` and `search_codebase("生年月日 有資格者リスト")`
found nothing, while `search_codebase("register")` found the same component. Two
properties of the FTS5 trigram index were never compensated for in the query layer:

- FTS5 joins bare terms with an implicit **AND**, so a multi-word query only matched
  a component containing every word verbatim — 0 results as soon as the words lived
  in sibling components.
- A term shorter than **3 characters** matches nothing at all under the trigram
  tokenizer, silently. Japanese is full of 2-character words (`職員`, `番号`, `商談`),
  and one of them was enough to empty the result set.

`search()` now widens only as far as it has to — AND → OR → `LIKE` (the only path
that can reach a 1–2 character term) — and ranks results by how many query terms the
component actually matched. Queries are also NFKC-folded, so a full-width
`ＡｃｃｏｕｎｔＳｅｒｖｉｃｅ` matches the index, and Japanese punctuation between
keywords (`、` `。`) is treated as a separator instead of being searched for.
`search_codebase` labels a partial hit as `(matched 1/2 terms)`; explicit FTS5 syntax
(`Staff*`, quotes, `AND`/`OR`/`NOT`/`NEAR`) is still passed through verbatim.

**`sf_command(action="validate", class_names=…)` could not run**

It failed with `Nonexistent flag: --class-names`. That flag belongs to
`sf apex run test`; `sf project deploy start` spells the same thing as a repeatable
`--tests`. `class_names` is now mapped per command and implies
`--test-level RunSpecifiedTests` for deploy/validate unless `test_level` is given.
Passing it to an action that cannot run tests reports that instead of shelling out.

**`sf_command(action="describe")` reported `Org: unknown ()`**

On a CLI-level failure (no default org, bad alias, expired auth) the `--json` payload
carries no `result`, only `name`/`message`/`status`. The summarizer read `result` off
it, got an empty dict, and printed a **success** line for an org that was never
reached. All summarizers now check that shape first, and `describe` prints the fields
the CLI actually returned (alias, username, org ID, instance, connected status, API
version, expiry) instead of one guessed line.

**The prompt compactor corrupted pasted CLI output**

The `UserPromptSubmit` hook rewrites the prompt the model sees, and it applied its
prose rules to the whole thing, so pasted machine text arrived damaged:

- indentation inside a fenced block was collapsed (`re.sub(r"[ \t]{2,}", " ")`),
  turning pasted YAML or JSON into something that no longer parses;
- the English filler list deleted words *inside* quoted machine text — eslint's
  `'just' is assigned a value but never used` became `'' is assigned a value…`.

Fenced blocks, inline code spans, shell-prompt lines, JSON/lint rows, stack traces
and file paths are now stashed before any substitution runs and restored byte for
byte; indentation is never collapsed; and a prompt that is mostly machine text skips
compaction entirely (`is_code_heavy`).

**`get_class_skeleton` printed method bodies twice**

`_METHOD_SIG` also matched control flow — `else if (cond) {` parses as return type
`else`, name `if`. Such a match sits inside a method body, so the emit loop rewound
its output cursor and printed the enclosing body a second time, interleaved with
`/* Logic Hidden */` placeholders. Control-flow keywords are now excluded and nested
matches are skipped, so every body appears exactly once.

### Known issues

The skill-priority hooks of the third-party `salesforce-development` plugin can block
read-only log/query/anonymous-Apex commands. That plugin is outside rtk-sf and cannot
be fixed from here — remove its `PreToolUse` entry from `.claude/settings.json` if it
gets in the way.

---

## [0.10.1] — 2026-09-29

### Fixed

**Hooks could not deliver their message, and could break the tool call**

Both `PreToolUse` hooks blocked with exit 2 and wrote the message to stdout. Claude
Code takes an exit-2 blocking message from **stderr**, so the text was discarded and
the user saw only `hook error: No stderr output`:

- `bash_guard` blocked metadata pipeline scans without ever showing which MCP tool
  to use instead — the redirect that is the hook's entire purpose.
- `ocr_intercept` blocked the Read without returning the OCR text.

Both now use the documented structured form: exit 0 with a `permissionDecision` of
`deny` on stdout, whose `permissionDecisionReason` reaches Claude intact.

**A noisy OCR stack made images unreadable**

`ocr_intercept` let PaddleOCR and torch write model-loading notices and
`UserWarning`s to stderr — some from native code, which `redirect_stderr` cannot
catch. Any stderr output is reported as a hook error, so on a machine where the OCR
engine fails to initialise, reading *any* image failed. The OCR call now runs with
file descriptors 1 and 2 pointed at /dev/null, and an empty transcription falls back
to the native Read instead of returning nothing.

### Added

- `tests/test_hooks_ocr_intercept.py` and `tests/test_hooks_bash_guard.py` (14 tests)
  pin the protocol: allow paths stay silent, blocks carry their reason, engine noise
  never reaches stderr, and a failing engine falls back rather than breaking the Read.

---

## [0.10.0] — 2026-09-29

### Added

**Living memory (`rtk_sf/memory/`)**
- `.rtk-sf/history.json` with a self-compacting bucket cascade: full-detail turn events for
  72 hours → per-day summaries → per-fiscal-quarter → per-fiscal-year. Writing an event
  triggers the roll-up, so the file never grows without bound.
- Fiscal-aware bucketing (`time_utils.py`), defaulting to an April fiscal-year start
  (Japanese convention) and configurable per project; the start month is recorded in the
  file so buckets stay consistent across runs.
- `last_7_days`: a derived, zero-filled per-day time series. Day summaries inside the
  trailing week are retained at day granularity even after the calendar month turns over,
  so "what happened last week" stays answerable on the 1st of a month.
- Atomic writes, and defensive reads: a missing or corrupt history file yields an empty
  store rather than raising, because this runs inside hooks that must not break a turn.

**Documentation engine (`rtk_sf/docgen/`)**
- `export_system_documentation(doc_type, output_dir)` MCP tool and `rtk-sf docs` CLI command,
  writing Markdown/Mermaid directly to disk and returning only a one-line confirmation —
  the document body never enters the agent's context window.
- Nine generators: `FUNCTION_MATRIX.md`, `FUNCTION_USECASES.md`, `SEQUENCE_DIAGRAMS.md`,
  `BUSINESS_SCENARIOS.md`, `OBJECT_DEFINITIONS.md`, `METADATA_INVENTORY.md`,
  `SCREEN_LIST.md`, `ERD.mmd`, `SYSTEM_DOCUMENT.md`.
- `extract.py` derives what the YAML spec index does not carry: Apex annotations
  (`@AuraEnabled`, `cacheable`, `@InvocableMethod`, REST), per-method DML/SOQL with the
  resolved sObject and access mode (`as user`, `WITH USER_MODE`, `WITH SECURITY_ENFORCED`),
  CRUD/FLS checks, thrown exceptions, call graphs, LWC→Apex imports, `@wire` adapters,
  published/handled events, and Redux Toolkit `createSlice`/`createAsyncThunk`/`createApi`
  constructs — Redux lives outside the metadata index entirely and is scanned directly.
- Apex attribution is scoped per construct: a file importing several Apex methods no longer
  credits all of them to every thunk and endpoint in that file.
- Facts that cannot be derived are marked `_undetermined_`, and facts positively established
  as absent render as `—`. Nothing is invented to fill a section.
- `get_project_timeline(scope)` MCP tool and `rtk-sf timeline` CLI command.

**Hooks**
- `memory_pre_turn.py` (UserPromptSubmit) injects a ≤150-token digest of the last 72 hours as
  `additionalContext`, so it composes with `compact_prompt` instead of fighting it over the
  prompt body.
- `memory_post_turn.py` (Stop) records the turn's git delta, keeping a per-file snapshot in
  the history so repeated turns do not re-log the same cumulative diff; a turn that changed
  nothing writes nothing.
- `rtk-sf install` now wires both, merging into `.claude/settings.json` idempotently.

### Notes
- The design for this release specified TypeScript modules under `src/mcp/` with Handlebars
  templates. rtk-sf is a Python package whose MCP server is Python, so the same design is
  implemented in Python under `rtk_sf/`; documents are assembled with plain string building
  to avoid adding a template-engine dependency.

---

## [0.9.0] — 2026-09-28

### Added

**`nl_to_soql` MCP tool (`rtk_sf/soql_compiler.py`)**
- Deterministic natural-language-to-SOQL compiler ported from furuCRM-Inc/flash-agent-stack's
  "Jev" engine, keeping only the fully LLM-free layer: regex/keyword extraction of filter
  conditions (dates incl. compound 億/千万/万 numerals, amounts, stage/case/lead status,
  null-checks, parent-account context, simple record updates) plus a schema-validated
  SOQL/SOSL compiler (quote-escaping, LIMIT clamping, `WITH USER_MODE`, graceful degrade
  when a field isn't locally indexed).
- `RECORD_UPDATE` matches are always returned as a proposal, never auto-executed as DML.
- 56 unit tests in `tests/test_soql_compiler.py`, ported from the source engine's own
  `jev-intent.test.ts` / `soqlCompiler.test.ts` cases to pin parity.

### Fixed

- **Indexer**: `_build_field_spec` was overwriting every field's real Salesforce type
  (Currency, Picklist, DateTime, ...) with the literal string `"CustomField"` before
  writing the YAML spec, so `get_object_schema` (and anything built on it) never saw
  actual field types. The real type is now preserved under `field_type`.
- **Schema validation**: fields guaranteed to exist on an object but never individually
  customized (Id, Name, CreatedDate, OwnerId, and common per-object standard fields like
  Opportunity.Amount / Account.AnnualRevenue) have no field-meta.xml of their own and were
  invisible to the local index's `valid_fields`, so conditions built on them — including
  `nl_to_soql`'s own "recent records" default filter — were silently dropped by schema
  validation. `soql_compiler.with_standard_fields()` now unions in a conservative,
  per-object allowlist before validating.

### Changed

- `data_tools.get_object_schema` refactored to share its spec-loading logic via the new
  `load_object_field_rows()`, reused directly by `nl_to_soql` for field validation.
- Root `CLAUDE.md` documents `nl_to_soql` and when to prefer it over a hand-written
  `soql_query` call.

---

## [0.1.0] — 2026-09-06

### Added

**Core indexer (`rtk_sf/indexer.py`)**
- Differential mtime tracking via `.rtk-sf/registry.json`
- Apex class parsing: class name, ApexDoc summary, method signatures (regex-based, no AST dependency)
- Custom field XML parsing: fullName, type, label, required, description, relationship metadata
- Custom object XML parsing: label, fields list, lookup relationships
- Salesforce Flow XML parsing: label, process type, status, element counts
- Compressed YAML spec output to `.rtk-sf/specs/<ComponentName>.yaml`
- Relations graph builder: nodes + edges in `.rtk-sf/relations.json`
- Reference extraction from Apex source (heuristic identifier analysis)
- CLI: `python -m rtk_sf index [--path ./force-app] [--force]`

**Search engine (`rtk_sf/search.py`)**
- SQLite FTS5 full-text search (no external dependencies)
- `search(query, limit)` — keyword search with snippet generation
- `get_spec(component_name)` — direct YAML spec retrieval
- `get_relations(component_name)` — upstream/downstream graph lookup
- `list_components(type)` — filtered component listing
- `sync_from_specs()` — populate DB from disk specs
- Optional vector re-ranking using bag-of-words cosine similarity (requires `numpy`)
- Upsert semantics for incremental updates

**MCP server (`rtk_sf/mcp_server.py`)**
- MCP 2024-11-05 protocol over stdio (JSON-RPC 2.0)
- Tool: `query_compressed_spec(component_name)` → YAML
- Tool: `search_codebase(query, limit)` → ranked results
- Tool: `get_relations(component_name)` → callers + deps
- Tool: `list_components(type)` → indexed components
- Compatible with `claude mcp add rtk-sf -- python -m rtk_sf serve`
- Graceful error handling; all diagnostics to stderr

**File watcher (`rtk_sf/watcher.py`)**
- `watchdog`-based live file monitoring
- Incremental re-index on `.cls` and `.xml` file changes
- Daemon mode: `python -m rtk_sf watch`
- Graceful startup: runs initial sync before watching

**Architecture map generator (`rtk_sf/ui_generator.py`)**
- Self-contained SPA: `dist/architecture_map.html` (no web server required)
- Cytoscape.js CoSE layout via CDN
- Node color coding by type (Apex/Object/Field/Flow)
- Click-to-view: YAML spec in right sidebar
- Path highlighting: selected (amber), upstream (light amber), downstream (red)
- Search box for filtering/dimming nodes
- Re-layout button
- Keyboard shortcuts: `Esc`, `Ctrl+K`, `F`
- furuCRM dark theme branding

**CLI (`rtk_sf/__main__.py`)**
- Subcommands: `index`, `watch`, `serve`, `ui`
- `--project-root` flag for non-CWD projects
- `--verbose` flag for debug logging
- `--version` flag

**Project**
- MIT License (furuCRM Inc. 2026)
- `pyproject.toml` with Hatchling build backend
- `requirements.txt`
- GitHub issue templates (bug report, feature request)
- PR template
- Installation script (`scripts/install.sh`)
- Documentation: `docs/installation.md`, `docs/mcp-integration.md`, `docs/roi.md`

---

[Unreleased]: https://github.com/furuCRM-Inc/rtk-sf/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/furuCRM-Inc/rtk-sf/releases/tag/v0.1.0
