# Documentation Engine & Living Memory

Two subsystems added in v0.10.0:

- **`rtk_sf/docgen/`** — generates the system document set to disk from the index plus the
  source files it points at.
- **`rtk_sf/memory/`** — a self-compacting project history (`.rtk-sf/history.json`) that
  answers "what have we been working on?" without replaying a transcript or reading git log.

---

## Quick start

```bash
# Index first — the engine reads .rtk-sf/specs
python3 -m rtk_sf index

# Whole document set into ./docs
python3 -m rtk_sf docs

# One document, custom destination
python3 -m rtk_sf docs sequence_diagrams --output-dir ./architecture

# Living memory
python3 -m rtk_sf timeline last_7_days
```

From an agent, via MCP:

```text
export_system_documentation(doc_type="all", output_dir="./docs")
get_project_timeline(scope="last_7_days")
```

`export_system_documentation` returns a single confirmation line. The documents are written
to disk, so a 40-page set costs the agent a handful of tokens instead of the whole body.

---

## The documents

| `doc_type` | Output | What it contains |
|---|---|---|
| `function_matrix` | `FUNCTION_MATRIX.md` | Every entry point wired to its LWC callers, thunks and RTK endpoints; objects touched; security markers; a gap list of uncalled entry points and unresolved client imports |
| `function_usecases` | `FUNCTION_USECASES.md` | Per entry point: actors, pre-conditions, main flow, exception/alternative flows, post-conditions and side effects |
| `sequence_diagrams` | `SEQUENCE_DIAGRAMS.md` | Mermaid sequence diagrams — LWC → Redux/RTK → Apex → DB, plus DML → trigger → handler chains |
| `business_scenarios` | `BUSINESS_SCENARIOS.md` | End-to-end journeys per primary object, with compliance and security requirements |
| `object_definitions` | `OBJECT_DEFINITIONS.md` | Objects, fields, picklist values, formulas, validation rules, record types |
| `metadata_inventory` | `METADATA_INVENTORY.md` | All indexed metadata mapped to its consumers |
| `screen_list` | `SCREEN_LIST.md` | LWC screens, host FlexiPages, layouts, navigation |
| `erd` | `ERD.mmd` | Raw Mermaid ER diagram (unfenced, ready for the Mermaid CLI) |
| `system_doc` | `SYSTEM_DOCUMENT.md` | Architecture overview, security posture, project timeline, links to the set |
| `all` | all of the above | |

---

## Where the facts come from

The YAML spec index is **structural**: it knows a class has a method, not that the method is
`@AuraEnabled` or updates Opportunity in user mode. `docgen/extract.py` re-reads the source
files the index points at and derives the rest:

| Layer | Derived signals |
|---|---|
| Apex | annotations (`@AuraEnabled`, `cacheable`, `@InvocableMethod`, REST), per-method SOQL/DML with resolved sObject and access mode, CRUD/FLS describe checks, thrown exceptions, try/catch, callouts, cross-class call graph, sharing keyword |
| LWC | `@salesforce/apex` imports, `@salesforce/schema` references, `@wire` adapters, published `CustomEvent`s, template handler bindings, store dispatches, toast/navigation usage |
| Redux | `createSlice` reducers, `createAsyncThunk` action types, `createApi` endpoints and their kind — scanned from the project tree, since Redux is not in the Salesforce metadata index at all |
| Schema | fields with types, required flags, formulas and picklist value sets (read from field XML), validation rules, record types, lookups |
| Human | `annotate_component` entries, surfaced verbatim as business context |

Two rendering rules keep the output trustworthy:

- A fact that **cannot** be derived is printed as `_undetermined_`.
- A fact positively established as **absent** (a method that performs no DML) renders as `—`.

Neither is ever replaced with plausible prose. If a section looks thin, the source is thin.

Apex attribution is scoped to the construct that references an import: a file importing three
Apex methods does not credit all three to every thunk and endpoint it contains.

---

## Living memory

`.rtk-sf/history.json` holds a cascade, each level an aggregate of the one before:

```text
recent_3_days    full-detail turn events, < 72h old
current_month    one summary per day (days inside the trailing week are kept here
                 even after the month turns over)
fiscal_quarters  one summary per quarter, current fiscal year
fiscal_years     one summary per fiscal year, everything older
last_7_days      derived, read-only: zero-filled per-day series over the trailing week
```

Writing an event triggers the roll-up, so the file compacts itself and never grows without
bound. The fiscal year defaults to an **April** start (FY2026 = 2026-04-01 → 2027-03-31) and
is recorded in the file, so changing the default later cannot silently re-bucket old data.

`last_7_days` exists because the bucket hierarchy otherwise jumps from 72 hours to the current
calendar month: on October 2nd, the previous week's late-September days would already have
been folded into a quarter summary and "last week" would be unanswerable. Trailing-week days
are therefore retained at day granularity until the week has fully passed.

### Hooks

`rtk-sf install` wires both hooks into `.claude/settings.json`:

| Hook | Event | Behavior |
|---|---|---|
| `rtk_sf.hooks.memory_pre_turn` | `UserPromptSubmit` | Injects a ≤150-token digest of the last 72 hours as `additionalContext` |
| `rtk_sf.hooks.memory_post_turn` | `Stop` | Records the turn's git delta (files, insertions, deletions, new commits) |

The design named these "pre-turn" and "post-turn"; those are not Claude Code events, so they
are bound to the real ones. The pre-turn hook adds context rather than rewriting the prompt,
so it composes with `compact_prompt`, which does rewrite it.

The post-turn hook keeps a per-file diff snapshot inside the history file and records only
what moved since the previous turn — otherwise every turn would re-log the same cumulative
working-tree diff. A turn that changed nothing writes nothing.

Both hooks exit 0 unconditionally: a memory failure must never interrupt a session. Activity
is visible via `rtk-sf hook-stats`.

---

## Regenerating

Documents carry a banner marking them generated. Local edits are overwritten on the next
export — put durable knowledge in `annotate_component` instead, where it is indexed,
searchable, and picked up by the use-case and scenario generators.
