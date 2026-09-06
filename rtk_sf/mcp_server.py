"""
mcp_server.py — MCP stdio JSON-RPC server for rtk-sf.

Implements the Model Context Protocol (MCP) over stdio so that AI agents
such as Claude Code and Cline can query Salesforce metadata specs without
reading raw source files.

Tools exposed:
    query_compressed_spec(component_name)  → YAML spec string
    search_codebase(query, limit)          → FTS5 search results
    get_relations(component_name)          → upstream/downstream graph
    list_components(type)                  → list of indexed components

Protocol:
    - Reads JSON-RPC 2.0 requests line-by-line from stdin.
    - Writes JSON-RPC 2.0 responses line-by-line to stdout.
    - Logs diagnostics to stderr (never stdout).

Usage:
    python -m rtk_sf serve [--project-root .]
    claude mcp add rtk-sf -- python -m rtk_sf serve
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
                    return self._format_spec_with_annotations(
                        f"# Closest match: {component_name}\n\n{spec}",
                        annotations,
                    )
            return (
                f"Component '{component_name}' not found in index.\n"
                "Run `rtk-sf index` to update the index."
            )

        annotations = search.get_annotations(component_name)
        return self._format_spec_with_annotations(spec, annotations)

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
