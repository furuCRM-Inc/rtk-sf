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

## [0.11.0] — 2026-10-03

Adds hybrid orchestration: Apex work is routed method-by-method between a local
code model and Claude, with a structural gate that lets Claude trust a verdict
instead of reading generated code. Three MCP tools: `hybrid_plan`,
`hybrid_delegate`, `hybrid_review`.

No new dependencies — the worker client is stdlib `urllib`. The documented
upgrade command keeps `--no-deps`.

### Added

**Deterministic method-level routing (`rtk_sf/hybrid/complexity.py`)**

Each method is scored from its own source, never from a hand-written
`complexity: LOW` label — a label drifts the moment the code changes, and a
stale one routes a high-blast-radius method to a 7B. Spans come from
`skeleton.py`'s comment- and string-aware brace scanner, so a brace inside
`'a { b'` cannot shift a body boundary.

- **score** — cyclomatic-style, weighted for SOQL, DML, nesting and length.
- **blockers** force HIGH regardless of score: SOQL/DML in loops,
  `Savepoint`/rollback, partial DML, callouts, `without sharing`, FLS/CRUD
  checks, batch Apex, email send. A three-line method that opens a Savepoint is
  not simple at any length.
- **flags** add weight without vetoing the route. `@AuraEnabled` lives here:
  measured against a real 179-method project, treating it as a blocker forced
  60 methods (34%) to Claude, which makes delegation pointless on any
  LWC-based project.
- **constructors are excluded.** `_METHOD_SIG` cannot express "has no return
  type", so a constructor matches only by capturing an access modifier as its
  return type. A worker told to keep the signature will "fix" that by adding
  one, silently converting a constructor into a method. 20 such spans exist in
  the verification corpus.

**Splice gate (`rtk_sf/hybrid/gate.py`)**

Nothing reaches a `.cls` file until the completion is untruncated, parses as
exactly one method with the requested name, balances braces/parens/literals
(comment- and string-aware — `code.count("{")` miscounts any brace inside a
literal and would approve broken source), triggers no ERROR-level
`dry_run` rule, and — the check that earns the right not to read the output —
leaves **every other method body byte-identical**, none added, none removed.
That last one is what catches a model that rewrote the whole class and dropped
three methods.

The gate is structural, not semantic. It cannot tell you the logic is right and
does not claim to; run the class's tests for behaviour.

**Handover state (`rtk_sf/hybrid/state.py`)**

Lives in `.rtk-sf/handover/`, **not** `.rtk-sf/specs/`. Spec files are
generated: `Indexer._write_spec` overwrites them wholesale and `watcher.py`
reindexes on every save, so state written into a spec is erased by the very
edit it was recording. Writes are atomic (`os.replace`) and
`allow_unicode=True`, so Japanese summaries stay readable instead of becoming
`\uXXXX`. `render_delta(since)` returns only records newer than the caller's
watermark.

**Correction memory — in-context learning, no finetune**

Failures become few-shot examples in two tiers. **Active** (while unresolved):
the last 2 critiques verbatim, each with the failing *method body*. **Promoted**
(on success): bad code dropped, critique distilled into a one-line, code-free,
deduplicated project rule that applies to every other method.

Storing the whole file per failure does not fit: three failures on a 300-LOC
class is ~12,600 tokens of wrong code against a worker window that defaults to
4,096. Keeping every failure forever also biases a small model toward the wrong
shapes it keeps being shown, while deleting on success throws away the only
durable artifact. Gate failures are recorded automatically at zero Claude cost;
`hybrid_review` is for semantic findings the gate cannot see.

**Worker client (`rtk_sf/hybrid/worker.py`)**

Ollama-compatible, stdlib only, lazily imported and opt-in — rtk-sf still runs
fully offline with no model calls. Configuration: `RTK_SF_WORKER_URL`,
`RTK_SF_WORKER_MODEL` (also tool arguments).

Pin the model for a finetune. Auto-detection ranks by known coder-model name
substrings, so a custom tag such as `qwen2.5-coder-7b-nexusmesh` matches
nothing and is rejected as "no code-tuned model" despite being the right
worker.

Measured against qwen2.5-coder:7b (Q4_K_M) on Ollama 0.21.2, Apple silicon,
and encoded as defaults:

| Property | Measured | Consequence |
|---|---|---|
| Throughput | 13.3 tok/s | default timeout 600 s — a 60 s timeout aborts a whole-file task *after* paying its compute |
| Served context | 4,096 (trained 32,768) | `num_ctx` always set explicitly; unset means silent truncation of prompt *and* completion |
| Markdown fences | emitted despite explicit instruction | fences stripped by the parser, not trusted to the prompt |
| Model tag | `qwen2.5-coder:7b` | the tag is kept whole; `split(":")[-1]` yields `"7b"`, which 404s every call |
| Unreachable host | 3.01 s per probe | detection cached per process (30 s negative / 300 s positive) — the MCP server builds a fresh orchestrator per call |

### Fixed

**`rtk-sf install` downgraded a project's CLAUDE.md
([#35](https://github.com/furuCRM-Inc/rtk-sf/issues/35))**

`_patch_claude_md` wrote a template hardcoded at v0.5.1 — 14 tools out of the
31 the package registers — and decided whether a file was current by looking
for the prose marker `"Image / screenshot rule"`, last updated in that release.
Any newer CLAUDE.md failed that check, so install concluded it was *older* and
overwrote it, silently deleting the guidance for `nl_to_soql`,
`get_record_types`, `get_lwc_targets`, `export_system_documentation`,
`get_project_timeline` and all five pipeline-scan guards. Same defect class as
[#31](https://github.com/furuCRM-Inc/rtk-sf/issues/31): a hand-maintained
version marker that stopped being maintained.

- The tool table is now **generated from the live tool registry**, so it cannot
  drift from what the package serves. A test asserts every registered tool
  appears in it, which is what keeps it true as tools are added.
- The block is delimited by `<!-- rtk-sf:begin <version> -->` and compared
  **semantically**. A file written by a newer rtk-sf is left untouched, and a
  re-run on a current file is a no-op. A version stamp can express "newer than
  me"; a prose marker never could.
- `CLAUDE.md.rtk-bak` is written before any change, and the block is replaced
  **in place** — the previous code re-inserted it after line 1, reordering the
  document. A file with no rtk-sf block is appended to rather than displaced.

**`rtk-sf update --help` performed the update instead of printing help**

The pre-parse intercept in `main()` uses `add_help=False` so it can tolerate
unknown flags on older installs, which also meant it swallowed `-h`/`--help`
and ran the upgrade — then re-ran `install`, which is how #35 was triggered.
Help flags are now handled before anything executes.

**`install` wrote one developer's interpreter path into a shared config**

`_patch_claude_settings` baked `sys.executable` into `.claude/settings.json`
hooks. That is correct inside a venv, but the file is commonly committed, so the
path of whichever interpreter happened to run install reached the whole team —
observed in #35 as an unrelated ESP-IDF environment that was merely first on
`PATH`. The hook command now uses a portable `python3` when that interpreter can
import `rtk_sf`, falling back to the absolute path only when it genuinely
cannot. The file also keeps its trailing newline.

### Token impact

On a real 206-line controller: `hybrid_plan` returns ~351 tokens where reading
the class costs ~2,861 (**88% fewer**); a handover delta is ~63 tokens against
~502 for the full record (**87% fewer**).

Note on prompt caching: splitting a spec into a "static prefix" and a "dynamic
tail" does **not** preserve Claude's cache. Caching is a prefix match over the
rendered request (`tools` → `system` → `messages`), and a file arrives as a tool
result appended at the *end* of `messages` — so re-reading it appends a full
fresh copy regardless of where the volatile section sits inside the file.
Measured over 6 simulated turns, static-first and dynamic-first layouts append
**byte-identical** totals (11,844 B each); delta reads append 534 B, 95% less.
What reduces cost is returning fewer tokens, which is what the delta read,
method-scoped prompts and batched results do. Rationale and the three
cache constraints that shape the tool surface: `docs/hybrid-orchestration.md`.

### Testing

30 tests in `tests/test_hybrid.py`, including a corpus-level regression that
runs every method in a real 25-file project through the splice path and
requires the file back byte-identical. That test is what caught two defects a
synthetic fixture missed: lost method indentation (valid Apex, so no syntax
check would ever flag it) and constructors being routed to the worker.

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
