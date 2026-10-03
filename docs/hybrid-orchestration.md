# Hybrid orchestration (local worker + Claude)

Routes Apex work method-by-method between a local code model and Claude. Three
MCP tools: `hybrid_plan`, `hybrid_delegate`, `hybrid_review`.

## The token model, corrected

The intuitive design is to split a spec file into a "static prefix" and a
"dynamic tail" so that appending state does not disturb Claude's KV cache.
**This does nothing.** Claude's prompt cache is a prefix match over the
rendered request (`tools` -> `system` -> `messages`). A file's contents arrive
as a tool result appended at the *end* of `messages`. Re-reading a file appends
a fresh full copy, billed as new input, no matter where the volatile section
sits inside the file. Key order inside a YAML file is a readability choice, not
a caching one.

Four things do reduce cost, and they are what this module implements:

1. **Method-scoped delegation.** The worker receives one method plus a
   collapsed class skeleton — never the whole file. It cannot exceed its
   context window and cannot rewrite the class.
2. **Verdicts instead of code.** Claude reads a gate result, not a diff. This
   is only sound because `gate.py` proves the splice touched exactly one
   method; without the gate, "the worker completed it" is an unverified claim
   and the saving is bought with correctness.
3. **Delta state reads.** `HandoverState.render_delta(since)` returns only
   records newer than the caller's watermark — tens of tokens per turn.
4. **Batched calls.** One tool result per batch. A turn that appends more than
   about 20 positions can push the previous cache entry out of the lookback
   window and force the whole conversation to be rewritten, so one tool call
   per method is actively harmful.

Two further caching constraints shape the design: tool definitions render at
position 0, so adding or reordering tools invalidates every cache tier (hence
three stable tools, not one per operation), and caches are model-scoped, so
nothing is shared between Claude and the local worker. The saving comes from
Claude never seeing the generated code, not from caching.

## Where state lives

`.rtk-sf/handover/<Component>.yaml`, **not** `.rtk-sf/specs/<Component>.yaml`.
Spec files are generated: `Indexer._write_spec` overwrites them wholesale and
`watcher.py` reindexes on every save. State written into a spec would be erased
by the very edit it was recording.

## Routing

`complexity.py` scores each method from its own source — never from a
hand-written `complexity: LOW` label, which drifts the moment the code changes.

* **score** — cyclomatic-style, weighted for SOQL, DML, nesting, and length.
* **blockers** — force HIGH regardless of score: SOQL/DML in loops,
  Savepoint/rollback, partial DML, callouts, `without sharing`, FLS/CRUD
  checks, batch Apex, email send. A three-line method that opens a Savepoint is
  not simple at any length.
* **flags** — add weight without vetoing the route. `@AuraEnabled` lives here:
  measured against a production Salesforce project, treating it as a blocker forced 60 of 179 methods
  to Claude on a normal LWC project, which makes delegation pointless.
* **constructors and non-splice-safe spans** are excluded. `_METHOD_SIG` cannot
  express "has no return type", so a constructor matches only by capturing an
  access modifier as its return type; a worker told to keep the signature will
  "fix" that by adding a return type, silently converting a constructor into a
  method. 20 such spans exist in that project.

## The gate

Nothing reaches a `.cls` file until all of this holds:

1. the completion is not truncated;
2. it parses as exactly one method, with the requested name;
3. braces, parens and string literals balance — checked comment- and
   string-aware, because `code.count("{")` miscounts a brace inside `'a { b'`;
4. splicing changes that one method and nothing else — every other body hash
   byte-identical, none added, none removed;
5. no ERROR-level rule from `dry_run._APEX_RULES` fires.

Check 4 is the one that earns the right not to read the code: it catches a
model that rewrote the whole class and dropped three methods.

The gate is structural, not semantic. It cannot tell you the logic is right and
never claims to — run the class's tests for that.

## Correction memory

Failures become in-context examples rather than finetuning data, in two tiers:

* **Active** (while unresolved): the last 2 critiques verbatim, each with the
  failing *method body*. Storing the whole file instead is what makes this
  mechanism unaffordable — three failures on a 300-LOC class is ~12,600 tokens
  of wrong code against a worker window that defaults to 4,096.
* **Promoted** (on success): the bad code is dropped and the critique is
  distilled into a one-line, code-free, deduplicated project rule (~10 tokens)
  that applies to every other method.

Neither extreme works. Keeping everything grows without bound and biases a
small model toward the wrong shapes it keeps being shown; deleting on success
throws away the only durable artifact, so the next method repeats the mistake.

Gate failures are recorded automatically at zero Claude cost; `hybrid_review`
is for semantic findings the gate cannot see.

## Configuration

| Setting | Env var | Default |
|---|---|---|
| Worker daemon URL | `RTK_SF_WORKER_URL` | `http://localhost:11434` |
| Worker model tag | `RTK_SF_WORKER_MODEL` | best code-tuned model served |

Pin the model for a finetune: auto-detection ranks by known coder-model name
substrings, so a custom tag such as `qwen2.5-coder-7b-nexusmesh` matches
nothing and would be rejected as "no code-tuned model" despite being the right
worker. Both settings are also tool arguments.

The module is never imported at startup and only ever contacts the configured
host, so rtk-sf still works fully offline with no model calls, per
CONTRIBUTING.md.

## Measured on this hardware

qwen2.5-coder:7b (Q4_K_M) via Ollama 0.21.2, Apple silicon:

| Property | Value |
|---|---|
| Generation throughput | 13.3 tok/s |
| Served context by default | 4,096 (trained: 32,768) |
| Emits markdown fences despite being told not to | yes |
| Typical method-scoped edit | 110-180 output tokens, 16-37 s |

Consequences encoded in `worker.py`: `num_ctx` is always set explicitly
(unset means silent truncation of both prompt and completion), fences are
stripped regardless of instructions, and the default timeout is 600 s — a
60 s timeout aborts a whole-file-sized task after paying its full compute.
