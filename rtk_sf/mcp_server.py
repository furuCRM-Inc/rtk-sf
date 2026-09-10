"""
mcp_server.py — MCP stdio JSON-RPC server for rtk-sf.

Implements the Model Context Protocol (MCP) over stdio so that AI agents
such as Claude Code and Cline can query Salesforce metadata specs without
reading raw source files.

Tools exposed:
    query_compressed_spec(component_name)       → YAML spec string
    search_codebase(query, limit)               → FTS5 search results
    get_relations(component_name)               → upstream/downstream graph
    list_components(type)                       → list of indexed components
    annotate_component(component_name, key, ..) → write discovered business logic
    get_class_skeleton(component_name, focus)   → surgical Apex class skeleton (v0.4.0)
    sf_command(action, ...)                     → silent sf CLI execution (v0.4.0)
    get_object_schema(object_name)              → compact data-generation profile (v0.4.1)
    soql_query(query, target_org, ..)           → SOQL with auto row truncation (v0.4.1)
    compact_prompt(text)                        → bilingual NLP prompt compactor (v0.5.0)
    validate_apex(code)                         → local Apex dry-run check (v0.5.0)
    validate_soql(query)                        → local SOQL dry-run check (v0.5.0)
    get_roi_stats()                             → session token/dollar savings report (v0.5.0)
    extract_image_text(image_path)             → local OCR — bypasses vision tokens (v0.5.0)

Protocol:
    - Reads JSON-RPC 2.0 requests line-by-line from stdin.
    - Writes JSON-RPC 2.0 responses line-by-line to stdout.
    - Logs diagnostics to stderr (never stdout).

Usage:
    python3 -m rtk_sf serve [--project-root .]
    claude mcp add rtk-sf -- python3 -m rtk_sf serve
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tool definitions (returned in initialize response)
# ---------------------------------------------------------------------------

_TOOLS: list[dict[str, Any]] = [
    {
        "name": "query_compressed_spec",
        "description": (
            "Return the compressed YAML specification for a Salesforce component "
            "(Apex class, custom object, custom field, or Flow). "
            "Use this instead of reading the raw source file — it is ~92% smaller."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "component_name": {
                    "type": "string",
                    "description": "Exact component name, e.g. 'AccountService' or 'Account__c'.",
                }
            },
            "required": ["component_name"],
        },
    },
    {
        "name": "search_codebase",
        "description": (
            "Search the indexed Salesforce codebase using keyword full-text search. "
            "Returns matching component names, types, and text snippets."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search keywords or component name fragment.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of results to return (default 5).",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_relations",
        "description": (
            "Return the upstream callers and downstream dependencies of a Salesforce component. "
            "Useful for blast-radius analysis before making a change."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "component_name": {
                    "type": "string",
                    "description": "Exact component name to look up in the relations graph.",
                }
            },
            "required": ["component_name"],
        },
    },
    {
        "name": "list_components",
        "description": (
            "List all components that have been indexed by rtk-sf, optionally filtered by type."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "type": {
                    "type": "string",
                    "description": (
                        "Component type filter: 'ApexClass', 'CustomObject', "
                        "'CustomField', 'Flow', or 'all' (default)."
                    ),
                    "enum": ["all", "ApexClass", "CustomObject", "CustomField", "Flow"],
                    "default": "all",
                }
            },
        },
    },
    {
        "name": "get_class_skeleton",
        "description": (
            "Return a surgical skeleton of an Apex class: class-level variables, "
            "constructor bodies, and any focus_methods are shown in full; all other "
            "method bodies are collapsed to '/* Logic Hidden */'. "
            "Use this instead of reading the raw .cls file — a 10,000-token class "
            "becomes ~300 tokens of context-perfect scaffold."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "component_name": {
                    "type": "string",
                    "description": "Apex class name, e.g. 'AccountService'.",
                },
                "focus_methods": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Method names whose bodies should be shown in full. "
                        "Constructors are always shown. All other methods are collapsed."
                    ),
                    "default": [],
                },
            },
            "required": ["component_name"],
        },
    },
    {
        "name": "sf_command",
        "description": (
            "Execute a Salesforce CLI command silently and return a condensed 1–4 line summary. "
            "Runs 'sf project deploy/retrieve' or 'sf apex run test' with --json in the background, "
            "parses the full response, and suppresses the raw output tables. "
            "Token impact: cuts terminal response overhead by ~99%."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "SF action to run.",
                    "enum": ["deploy", "retrieve", "run_test", "describe", "validate"],
                },
                "target_org": {
                    "type": "string",
                    "description": "Org alias or username (e.g. 'dev01').",
                },
                "source_dir": {
                    "type": "string",
                    "description": "Source directory path (e.g. 'force-app').",
                },
                "metadata": {
                    "type": ["string", "array"],
                    "description": "Metadata type(s) to deploy/retrieve (e.g. 'ApexClass:AccountService').",
                },
                "test_level": {
                    "type": "string",
                    "description": "Test level for deploy/run_test.",
                    "enum": ["NoTestRun", "RunLocalTests", "RunAllTestsInOrg", "RunSpecifiedTests"],
                },
                "class_names": {
                    "type": ["string", "array"],
                    "description": "Apex test class name(s) for run_test action.",
                },
                "wait": {
                    "type": "integer",
                    "description": "Minutes to wait for async operations (default: 10).",
                    "default": 10,
                },
            },
            "required": ["action"],
        },
    },
    {
        "name": "get_object_schema",
        "description": (
            "Return a compact data-generation profile for a Salesforce object "
            "from the local index — WITHOUT calling 'sf sobject describe'. "
            "Includes only field API names, data types, required flags, and picklist values. "
            "Token impact: 5,000-token live schema → ~150-token clean dictionary. "
            "Use this before creating or modifying records to know what fields to populate."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "object_name": {
                    "type": "string",
                    "description": "Salesforce object API name, e.g. 'Order__c' or 'Account'.",
                }
            },
            "required": ["object_name"],
        },
    },
    {
        "name": "soql_query",
        "description": (
            "Run a SOQL query against a Salesforce org with automatic row truncation. "
            "Enforces a LIMIT cap (default 3) and strips internal attributes from results, "
            "so Claude sees only sample data patterns — not a 500-record dump. "
            "Token impact: prevents thousands of result tokens flooding the context window. "
            "Use this instead of 'sf data query' for any data inspection task."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "SOQL query string, e.g. \"SELECT Id, Name, Status__c FROM Order__c\".",
                },
                "target_org": {
                    "type": "string",
                    "description": "Org alias or username (e.g. 'dev01').",
                },
                "sample_size": {
                    "type": "integer",
                    "description": "Maximum records to return (default 3, max recommended 10).",
                    "default": 3,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "compact_prompt",
        "description": (
            "Strip conversational noise from a bilingual (English + Japanese) prompt "
            "before routing it to the AI engine. Removes polite fillers, pleasantries, "
            "and grammatical particles while preserving Salesforce API names, method names, "
            "and all structural parameters. "
            "Token impact: a 200-token polite request → ~80-token intent payload."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "Raw user prompt text to compact (English, Japanese, or mixed).",
                }
            },
            "required": ["text"],
        },
    },
    {
        "name": "validate_apex",
        "description": (
            "Run a local regex-based dry-run check on Apex code before deploying to sandbox. "
            "Detects: SOQL/DML inside loops, System.debug calls, TODO comments, "
            "unbalanced braces, unclosed strings, and @future parameter type violations. "
            "Zero org calls — instant feedback."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "Apex class or method source code to validate.",
                }
            },
            "required": ["code"],
        },
    },
    {
        "name": "validate_soql",
        "description": (
            "Run a local regex-based dry-run check on a SOQL query before executing against an org. "
            "Detects: missing SELECT/FROM, SELECT *, LIMIT > 50,000, unbalanced parentheses, "
            "missing WHERE+LIMIT (full-table scan risk), and date literal syntax errors. "
            "Zero org calls — instant feedback."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "SOQL query string to validate.",
                }
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_roi_stats",
        "description": (
            "Return a visual terminal summary of tokens and dollars saved this session "
            "by using rtk-sf suppression tools instead of raw file reads or org calls. "
            "Tracks: query_compressed_spec, get_class_skeleton, get_object_schema, soql_query."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "extract_image_text",
        "description": (
            "Extract English and Japanese text from an image file using local OCR — "
            "bypassing Claude's multimodal vision token cost entirely. "
            "Uses PaddleOCR (primary) or EasyOCR (fallback). "
            "Supports: .png .jpg .jpeg .bmp .tiff .webp. "
            "Token impact: a 1280×800 screenshot costs ~1,600 vision tokens as an image; "
            "this returns the extracted text at ~100–300 tokens instead. "
            "IMPORTANT: requires a file path on disk. "
            "When the user pastes an image inline (no path given), ask them: "
            "'To avoid vision token cost, please share the file path (e.g. ~/Downloads/screenshot.png) "
            "so I can run local OCR instead.' "
            "Do NOT read inline images with native vision when a path can be obtained."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "image_path": {
                    "type": "string",
                    "description": "Absolute or relative path to the image file on disk. Not for inline/pasted images.",
                },
                "preprocess": {
                    "type": "boolean",
                    "description": (
                        "Apply grayscale + contrast boost before OCR. "
                        "Helps with low-contrast screenshots or dark-mode UIs (default true)."
                    ),
                    "default": True,
                },
            },
            "required": ["image_path"],
        },
    },
    # ── TypeScript track tools (v0.6.0) ────────────────────────────────────
    {
        "name": "get_ts_skeleton",
        "description": (
            "Return a compressed structural skeleton of a TypeScript/JavaScript file. "
            "Shows imports, interfaces/types/enums (in full), class signatures and "
            "method signatures, export function signatures. "
            "Implementation bodies are replaced with { /* logic hidden */ }. "
            "Use instead of reading the raw .ts/.tsx/.js/.jsx file; saves ~85% of tokens."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Absolute or relative path to the .ts/.tsx/.js/.jsx file.",
                },
                "focus_names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Function/method names whose full bodies should be shown.",
                },
            },
            "required": ["file_path"],
        },
    },
    {
        "name": "run_js_tests",
        "description": (
            "Run a Jest/Vitest/Playwright test suite and return a compact summary. "
            "On success: single-line pass count. "
            "On failure: test name, expect() mismatch, and first source file frame only — "
            "all node_modules stack frames are stripped. "
            "Reduces a 300-line Jest log to ~10 lines."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "test_path": {
                    "type": "string",
                    "description": "File, directory, or pattern to pass to the test runner.",
                },
                "runner": {
                    "type": "string",
                    "enum": ["jest", "vitest", "playwright"],
                    "description": "Test runner to use (default: 'jest').",
                    "default": "jest",
                },
                "extra_args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Extra CLI flags, e.g. ['--testPathPattern=payment', '--coverage'].",
                },
                "cwd": {
                    "type": "string",
                    "description": "Working directory for the test command (default: current directory).",
                },
            },
            "required": [],
        },
    },
    # ── Kotlin track tools (v0.7.0) ────────────────────────────────────────
    {
        "name": "get_kotlin_skeleton",
        "description": (
            "Return a compressed structural skeleton of a Kotlin (.kt/.kts) file. "
            "Shows package, imports, interface/enum bodies (full), data class headers, "
            "class/object signatures, constructor bodies, and fun signatures — "
            "all implementation bodies replaced with { /* logic hidden */ }. "
            "Use instead of reading the raw .kt file; saves ~90% of tokens."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Absolute or relative path to the .kt or .kts file.",
                },
                "focus_names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Function/method names whose full bodies should be shown.",
                },
            },
            "required": ["file_path"],
        },
    },
    {
        "name": "run_gradle",
        "description": (
            "Run a Gradle task (build, test, assemble, etc.) and return a compact summary. "
            "On success: single-line confirmation with test count. "
            "On failure: Kotlin compiler errors (file:line:col + message) and "
            "JUnit/Kotest assertion mismatches only — all JVM framework stack frames stripped. "
            "Reduces a 600-line Gradle log to ~8 lines."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "Gradle task to run, e.g. 'test', 'build', 'assemble' (default 'build').",
                    "default": "build",
                },
                "project_dir": {
                    "type": "string",
                    "description": "Directory containing gradlew (default: current directory).",
                },
                "extra_args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Extra Gradle flags, e.g. ['--tests', 'com.example.FooTest', '--info'].",
                },
            },
            "required": [],
        },
    },
    # ── Python track tools (v0.5.0) ────────────────────────────────────────
    {
        "name": "get_python_skeleton",
        "description": (
            "Return a compressed structural skeleton of a Python file using AST analysis. "
            "Shows class names, base classes, method signatures with type hints, "
            "__init__ bodies, and docstrings — all other method bodies are hidden. "
            "Use instead of reading the raw .py file; saves ~90% of tokens."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Absolute or relative path to the .py file.",
                },
                "focus_methods": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Method names whose full bodies should be shown (others stay hidden).",
                },
            },
            "required": ["file_path"],
        },
    },
    {
        "name": "run_python_tests",
        "description": (
            "Run pytest on a path and return a compact summary. "
            "On success: single-line pass count. "
            "On failure: file path, line number, and AssertionError only — no noisy traceback. "
            "Reduces a 200-line pytest log to 5-10 lines."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "test_path": {
                    "type": "string",
                    "description": "File or directory to pass to pytest (default '.').",
                    "default": ".",
                },
                "extra_args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Extra pytest flags, e.g. [\"-x\", \"-k\", \"test_auth\"].",
                },
            },
            "required": [],
        },
    },
    {
        "name": "read_data_file",
        "description": (
            "Read a CSV, JSON, or JSONL data file and return a compact structural preview: "
            "schema (column names + types) plus up to 3 sample rows. "
            "Never dumps the full file — protects context from large datasets."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Path to the CSV, JSON, or JSONL file.",
                },
                "sample_rows": {
                    "type": "integer",
                    "description": "Maximum rows to return (default 3, max 10).",
                    "default": 3,
                },
            },
            "required": ["file_path"],
        },
    },
    {
        "name": "annotate_component",
        "description": (
            "Record a discovered business rule, condition, or context note on a component. "
            "Use this after finding logic in source code (e.g. an if-condition, SOQL filter, "
            "access rule) so the knowledge is searchable without re-reading the source file. "
            "Annotations are immediately indexed and returned by query_compressed_spec."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "component_name": {
                    "type": "string",
                    "description": "Exact component name to annotate (e.g. 'Application__c').",
                },
                "key": {
                    "type": "string",
                    "description": (
                        "Short category label for the annotation. "
                        "Examples: 'business_rule', 'condition', 'access_rule', 'soql_filter', 'validation'."
                    ),
                },
                "value": {
                    "type": "string",
                    "description": "Full description of the discovered logic or context.",
                },
                "source": {
                    "type": "string",
                    "description": "Origin of this annotation (default: 'ai_discovery').",
                    "default": "ai_discovery",
                },
            },
            "required": ["component_name", "key", "value"],
        },
    },
]


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------


class MCPServer:
    """
    MCP stdio JSON-RPC server.

    Reads from stdin, writes to stdout, logs to stderr.
    Compatible with the `claude mcp add` command.
    """

    SERVER_INFO = {
        "name": "rtk-sf",
        "version": "0.1.0",
        "description": "Zero-Token Knowledge Layer for Salesforce AI Agents",
    }

    def __init__(self, project_root: str | Path = ".") -> None:
        self.project_root = Path(project_root).resolve()
        self._search: Any = None

    def _get_search(self) -> Any:
        """Lazy-initialize the search engine."""
        if self._search is None:
            from rtk_sf.search import SearchEngine

            self._search = SearchEngine(self.project_root)
            # Ensure DB is populated from spec files
            self._search.sync_from_specs()
        return self._search

    # ------------------------------------------------------------------
    # JSON-RPC helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _ok(request_id: Any, result: Any) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }

    @staticmethod
    def _write(obj: dict[str, Any]) -> None:
        """Write a JSON-RPC message to stdout, flushing immediately."""
        line = json.dumps(obj, ensure_ascii=False)
        sys.stdout.write(line + "\n")
        sys.stdout.flush()

    # ------------------------------------------------------------------
    # Request handlers
    # ------------------------------------------------------------------

    def _handle_initialize(self, request_id: Any, _params: dict) -> None:
        self._write(
            self._ok(
                request_id,
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": self.SERVER_INFO,
                },
            )
        )

    def _handle_tools_list(self, request_id: Any) -> None:
        self._write(self._ok(request_id, {"tools": _TOOLS}))

    def _handle_tool_call(self, request_id: Any, params: dict) -> None:
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})

        try:
            if tool_name == "query_compressed_spec":
                result = self._tool_query_spec(arguments)
            elif tool_name == "search_codebase":
                result = self._tool_search(arguments)
            elif tool_name == "get_relations":
                result = self._tool_get_relations(arguments)
            elif tool_name == "list_components":
                result = self._tool_list_components(arguments)
            elif tool_name == "annotate_component":
                result = self._tool_annotate(arguments)
            elif tool_name == "get_class_skeleton":
                result = self._tool_get_skeleton(arguments)
            elif tool_name == "sf_command":
                result = self._tool_sf_command(arguments)
            elif tool_name == "get_object_schema":
                result = self._tool_get_object_schema(arguments)
            elif tool_name == "soql_query":
                result = self._tool_soql_query(arguments)
            elif tool_name == "compact_prompt":
                result = self._tool_compact_prompt(arguments)
            elif tool_name == "validate_apex":
                result = self._tool_validate_apex(arguments)
            elif tool_name == "validate_soql":
                result = self._tool_validate_soql(arguments)
            elif tool_name == "get_roi_stats":
                result = self._tool_get_roi_stats()
            elif tool_name == "extract_image_text":
                result = self._tool_extract_image_text(arguments)
            elif tool_name == "get_kotlin_skeleton":
                result = self._tool_get_kotlin_skeleton(arguments)
            elif tool_name == "run_gradle":
                result = self._tool_run_gradle(arguments)
            elif tool_name == "get_ts_skeleton":
                result = self._tool_get_ts_skeleton(arguments)
            elif tool_name == "run_js_tests":
                result = self._tool_run_js_tests(arguments)
            elif tool_name == "get_python_skeleton":
                result = self._tool_get_python_skeleton(arguments)
            elif tool_name == "run_python_tests":
                result = self._tool_run_python_tests(arguments)
            elif tool_name == "read_data_file":
                result = self._tool_read_data_file(arguments)
            else:
                self._write(self._error(request_id, -32601, f"Unknown tool: {tool_name}"))
                return

            self._write(
                self._ok(
                    request_id,
                    {"content": [{"type": "text", "text": result}]},
                )
            )
        except Exception as exc:
            logger.error("Error executing tool %s: %s", tool_name, exc, exc_info=True)
            self._write(self._error(request_id, -32000, str(exc)))

    def _tool_query_spec(self, args: dict) -> str:
        component_name = args.get("component_name", "").strip()
        if not component_name:
            return "Error: component_name is required."

        search = self._get_search()
        spec = search.get_spec(component_name)
        if spec is None:
            # Try fuzzy: search for the name and return first hit's spec
            results = search.search(component_name, limit=1)
            if results:
                best = results[0]
                spec = best.get("yaml_spec") or search.get_spec(best["name"])
                if spec:
                    component_name = best["name"]
                    annotations = search.get_annotations(component_name)
                    result = self._format_spec_with_annotations(
                        f"# Closest match: {component_name}\n\n{spec}",
                        annotations,
                    )
                    from rtk_sf.dry_run import record_savings
                    record_savings("query_compressed_spec", raw_tokens=4000, compressed_tokens=len(result) // 4)
                    return result
            return (
                f"Component '{component_name}' not found in index.\n"
                "Run `rtk-sf index` to update the index."
            )

        annotations = search.get_annotations(component_name)
        result = self._format_spec_with_annotations(spec, annotations)
        from rtk_sf.dry_run import record_savings
        record_savings("query_compressed_spec", raw_tokens=4000, compressed_tokens=len(result) // 4)
        return result

    @staticmethod
    def _format_spec_with_annotations(spec: str, annotations: list) -> str:
        if not annotations:
            return spec
        lines = [spec, "", "## Annotations (discovered business logic)", ""]
        for a in annotations:
            import datetime
            ts = datetime.datetime.fromtimestamp(a["created_at"]).strftime("%Y-%m-%d")
            lines.append(f"[{a['key']}] ({a['source']} · {ts})")
            lines.append(f"  {a['value']}")
            lines.append("")
        return "\n".join(lines)

    def _tool_get_object_schema(self, args: dict) -> str:
        object_name = args.get("object_name", "").strip()
        if not object_name:
            return "Error: object_name is required."
        from rtk_sf.data_tools import get_object_schema
        from rtk_sf.dry_run import record_savings
        rtk_dir = self.project_root / ".rtk-sf"
        result = get_object_schema(object_name, rtk_dir)
        record_savings("get_object_schema", raw_tokens=5000, compressed_tokens=len(result) // 4)
        return result

    def _tool_soql_query(self, args: dict) -> str:
        query = args.get("query", "").strip()
        if not query:
            return "Error: query is required."
        from rtk_sf.data_tools import run_soql
        from rtk_sf.dry_run import record_savings
        result = run_soql(
            query=query,
            target_org=args.get("target_org"),
            sample_size=int(args.get("sample_size", 3)),
        )
        record_savings("soql_query", raw_tokens=8000, compressed_tokens=len(result) // 4)
        return result

    def _tool_compact_prompt(self, args: dict) -> str:
        text = args.get("text", "").strip()
        if not text:
            return "Error: text is required."
        from rtk_sf.nlp_compactor import compact_prompt_report
        return compact_prompt_report(text)

    def _tool_validate_apex(self, args: dict) -> str:
        code = args.get("code", "").strip()
        if not code:
            return "Error: code is required."
        from rtk_sf.dry_run import validate_apex
        return validate_apex(code)

    def _tool_validate_soql(self, args: dict) -> str:
        query = args.get("query", "").strip()
        if not query:
            return "Error: query is required."
        from rtk_sf.dry_run import validate_soql
        return validate_soql(query)

    def _tool_get_roi_stats(self) -> str:
        from rtk_sf.dry_run import get_roi_stats
        return get_roi_stats()

    def _tool_extract_image_text(self, args: dict) -> str:
        image_path = args.get("image_path", "").strip()
        if not image_path:
            return "Error: image_path is required."
        preprocess = bool(args.get("preprocess", True))
        from rtk_sf.vision_ocr import extract_image_text
        from rtk_sf.dry_run import record_savings
        result = extract_image_text(image_path, preprocess=preprocess)
        if not result.startswith("[rtk-sf OCR Error") and not result.startswith("[rtk-sf OCR:"):
            # Successful extraction — record vision token savings
            # Baseline: typical screenshot ~1,500 vision tokens; extracted text ~100-300 tokens
            record_savings("extract_image_text", raw_tokens=1500, compressed_tokens=len(result) // 4)
        return result

    def _tool_annotate(self, args: dict) -> str:
        component_name = args.get("component_name", "").strip()
        key = args.get("key", "").strip()
        value = args.get("value", "").strip()
        source = args.get("source", "ai_discovery").strip() or "ai_discovery"

        if not component_name or not key or not value:
            return "Error: component_name, key, and value are all required."

        search = self._get_search()
        ok = search.add_annotation(component_name, key, value, source)
        if ok:
            return (
                f"Annotation saved on '{component_name}'.\n"
                f"  key    : {key}\n"
                f"  source : {source}\n"
                f"  value  : {value}\n\n"
                f"This is now searchable via search_codebase and included in query_compressed_spec."
            )
        return (
            f"Component '{component_name}' not found in index. "
            "Run `rtk-sf index` first."
        )

    def _tool_get_skeleton(self, args: dict) -> str:
        component_name = args.get("component_name", "").strip()
        focus_methods = args.get("focus_methods") or []
        if not component_name:
            return "Error: component_name is required."

        from rtk_sf.skeleton import skeleton_from_spec

        rtk_dir = self.project_root / ".rtk-sf"
        result = skeleton_from_spec(component_name, rtk_dir, focus_methods)
        if result is None:
            # Try fuzzy match via search
            search = self._get_search()
            hits = search.search(component_name, limit=1)
            if hits:
                best = hits[0]["name"]
                result = skeleton_from_spec(best, rtk_dir, focus_methods)
                if result:
                    from rtk_sf.dry_run import record_savings
                    record_savings("get_class_skeleton", raw_tokens=10000, compressed_tokens=len(result) // 4)
                    return f"# Closest match: {best}\n\n{result}"
            return (
                f"Component '{component_name}' not found or is not an Apex class.\n"
                "Run `rtk-sf index` to update the index."
            )
        from rtk_sf.dry_run import record_savings
        record_savings("get_class_skeleton", raw_tokens=10000, compressed_tokens=len(result) // 4)
        return result

    def _tool_sf_command(self, args: dict) -> str:
        action = args.get("action", "").strip()
        if not action:
            return "Error: action is required."

        from rtk_sf.sf_runner import run_sf_command

        run_args = {k: v for k, v in args.items() if k != "action"}
        return run_sf_command(action, run_args)

    def _tool_search(self, args: dict) -> str:
        query = args.get("query", "").strip()
        limit = int(args.get("limit", 5))
        if not query:
            return "Error: query is required."

        search = self._get_search()
        results = search.search(query, limit=limit)

        if not results:
            return f"No results found for '{query}'."

        lines = [f"Search results for '{query}':\n"]
        for i, r in enumerate(results, 1):
            lines.append(f"{i}. [{r['type']}] {r['name']}")
            if r.get("snippet"):
                lines.append(f"   {r['snippet']}")
            lines.append("")

        return "\n".join(lines)

    def _tool_get_relations(self, args: dict) -> str:
        component_name = args.get("component_name", "").strip()
        if not component_name:
            return "Error: component_name is required."

        search = self._get_search()
        relations = search.get_relations(component_name)

        lines = [f"Relations for: {component_name}\n"]
        upstream = relations.get("upstream", [])
        downstream = relations.get("downstream", [])

        if upstream:
            lines.append(f"Upstream callers ({len(upstream)}):")
            for name in upstream:
                lines.append(f"  - {name}")
        else:
            lines.append("Upstream callers: none")

        lines.append("")

        if downstream:
            lines.append(f"Downstream dependencies ({len(downstream)}):")
            for name in downstream:
                lines.append(f"  - {name}")
        else:
            lines.append("Downstream dependencies: none")

        return "\n".join(lines)

    def _tool_list_components(self, args: dict) -> str:
        component_type = args.get("type", "all")
        search = self._get_search()
        components = search.list_components(component_type)

        if not components:
            msg = "No components indexed yet."
            if component_type != "all":
                msg += f" (filtered by type: {component_type})"
            msg += "\nRun `rtk-sf index` to index your Salesforce project."
            return msg

        # Group by type
        by_type: dict[str, list[str]] = {}
        for c in components:
            by_type.setdefault(c["type"], []).append(c["name"])

        lines = [f"Indexed components ({len(components)} total):\n"]
        for ctype, names in sorted(by_type.items()):
            lines.append(f"{ctype} ({len(names)}):")
            for name in sorted(names):
                lines.append(f"  - {name}")
            lines.append("")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Kotlin track tools
    # ------------------------------------------------------------------

    def _tool_get_kotlin_skeleton(self, args: dict) -> str:
        file_path = args.get("file_path", "").strip()
        if not file_path:
            return "Error: file_path is required."
        focus_names = args.get("focus_names") or None

        from rtk_sf.kotlin.kt_skeletonizer import skeletonize_file
        return skeletonize_file(file_path, focus_names)

    def _tool_run_gradle(self, args: dict) -> str:
        task = args.get("task", "build").strip() or "build"
        project_dir = args.get("project_dir") or None
        extra_args = args.get("extra_args") or []

        from rtk_sf.kotlin.gradle_masker import run_build
        return run_build(task, project_dir, extra_args)

    # ------------------------------------------------------------------
    # TypeScript track tools
    # ------------------------------------------------------------------

    def _tool_get_ts_skeleton(self, args: dict) -> str:
        file_path = args.get("file_path", "").strip()
        if not file_path:
            return "Error: file_path is required."
        focus_names = args.get("focus_names") or None

        from rtk_sf.typescript.ts_skeletonizer import skeletonize_file
        return skeletonize_file(file_path, focus_names)

    def _tool_run_js_tests(self, args: dict) -> str:
        test_path = args.get("test_path") or None
        runner = args.get("runner", "jest")
        extra_args = args.get("extra_args") or []
        cwd = args.get("cwd") or None

        from rtk_sf.typescript.jest_masker import run_tests
        return run_tests(test_path, runner, extra_args, cwd)

    # ------------------------------------------------------------------
    # Python track tools
    # ------------------------------------------------------------------

    def _tool_get_python_skeleton(self, args: dict) -> str:
        file_path = args.get("file_path", "").strip()
        if not file_path:
            return "Error: file_path is required."
        focus_methods = args.get("focus_methods") or None

        from rtk_sf.python.ast_skeletonizer import skeletonize_file
        result = skeletonize_file(file_path, focus_methods)

        from rtk_sf.dry_run import record_savings
        raw_est = len(result) // 4
        record_savings("get_python_skeleton", raw_tokens=raw_est * 10, compressed_tokens=raw_est)
        return result

    def _tool_run_python_tests(self, args: dict) -> str:
        test_path = args.get("test_path", ".").strip() or "."
        extra_args = args.get("extra_args") or []

        from rtk_sf.python.pytest_masker import run_tests
        return run_tests(test_path, extra_args)

    def _tool_read_data_file(self, args: dict) -> str:
        file_path = args.get("file_path", "").strip()
        if not file_path:
            return "Error: file_path is required."
        sample_rows = min(int(args.get("sample_rows", 3)), 10)

        from pathlib import Path as _Path
        import json as _json

        p = _Path(file_path)
        if not p.exists():
            return f"File not found: {file_path}"

        suffix = p.suffix.lower()
        lines_out: list[str] = [f"# Data preview: {p.name}\n"]

        try:
            if suffix == ".csv":
                import csv
                with open(p, encoding="utf-8", errors="replace", newline="") as fh:
                    reader = csv.DictReader(fh)
                    rows = []
                    for i, row in enumerate(reader):
                        if i >= sample_rows:
                            break
                        rows.append(dict(row))
                    fieldnames = reader.fieldnames or []

                lines_out.append(f"Format : CSV")
                lines_out.append(f"Columns: {', '.join(fieldnames)} ({len(fieldnames)} total)")
                lines_out.append(f"Sample ({len(rows)} rows):")
                for row in rows:
                    lines_out.append(f"  {_json.dumps(row, ensure_ascii=False)}")

            elif suffix in {".json", ".jsonl"}:
                raw = p.read_text(encoding="utf-8", errors="replace")
                if suffix == ".jsonl":
                    records = [_json.loads(ln) for ln in raw.splitlines() if ln.strip()]
                else:
                    data = _json.loads(raw)
                    records = data if isinstance(data, list) else [data]

                sample = records[:sample_rows]
                total = len(records)
                lines_out.append(f"Format : {'JSONL' if suffix == '.jsonl' else 'JSON'}")
                lines_out.append(f"Records: {total} total, showing {len(sample)}")
                if sample and isinstance(sample[0], dict):
                    keys = list(sample[0].keys())
                    lines_out.append(f"Keys   : {', '.join(keys)}")
                lines_out.append("Sample:")
                for rec in sample:
                    lines_out.append(f"  {_json.dumps(rec, ensure_ascii=False)}")
            else:
                return f"Unsupported file type: {suffix}. Supported: .csv, .json, .jsonl"

        except Exception as exc:
            return f"Error reading {p.name}: {exc}"

        return "\n".join(lines_out)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def _dispatch(self, request: dict) -> None:
        """Dispatch a single JSON-RPC request."""
        request_id = request.get("id")
        method = request.get("method", "")
        params = request.get("params") or {}

        logger.debug("Request: method=%s id=%s", method, request_id)

        if method == "initialize":
            self._handle_initialize(request_id, params)
        elif method == "initialized":
            # Notification — no response needed
            pass
        elif method == "tools/list":
            self._handle_tools_list(request_id)
        elif method == "tools/call":
            self._handle_tool_call(request_id, params)
        elif method == "ping":
            self._write(self._ok(request_id, {}))
        elif method.startswith("notifications/"):
            # Client notifications — no response
            pass
        else:
            if request_id is not None:
                self._write(self._error(request_id, -32601, f"Method not found: {method}"))

    def serve(self) -> None:
        """
        Start the MCP stdio server loop.

        Reads newline-delimited JSON-RPC messages from stdin until EOF.
        """
        logger.info(
            "rtk-sf MCP server starting. Project root: %s", self.project_root
        )
        print(
            f"rtk-sf MCP server ready (project: {self.project_root})",
            file=sys.stderr,
        )

        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError as exc:
                logger.warning("Invalid JSON: %s — %s", exc, line[:200])
                self._write(self._error(None, -32700, f"Parse error: {exc}"))
                continue

            try:
                self._dispatch(request)
            except Exception as exc:
                logger.error("Unhandled error: %s", exc, exc_info=True)
                request_id = request.get("id")
                if request_id is not None:
                    self._write(self._error(request_id, -32000, str(exc)))

        logger.info("stdin closed; MCP server exiting.")
        if self._search:
            self._search.close()
