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
