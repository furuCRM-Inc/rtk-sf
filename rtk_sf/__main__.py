"""
__main__.py — CLI entry point for rtk-sf.

Supports subcommands:
    index   — Index a Salesforce DX project
    watch   — Watch for file changes and re-index incrementally
    serve   — Start the MCP stdio JSON-RPC server
    ui      — Generate the architecture map HTML

Usage:
    python -m rtk_sf <subcommand> [options]
    rtk-sf <subcommand> [options]      (when installed via pip)
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )


# ---------------------------------------------------------------------------
# Subcommand: index
# ---------------------------------------------------------------------------


def cmd_index(args: argparse.Namespace) -> int:
    """Index the Salesforce project metadata."""
    from rtk_sf.indexer import SalesforceIndexer
    from rtk_sf.search import SearchEngine

    project_root = Path(args.project_root).resolve()
    force_app = Path(args.path).resolve() if args.path else None

    print(f"rtk-sf indexer starting...")
    print(f"  Project root : {project_root}")
    print(f"  Source path  : {force_app or project_root / 'force-app'}")

    indexer = SalesforceIndexer(project_root)
    counters = indexer.index_project(force_app)

    print(f"\nIndexing complete:")
    print(f"  Indexed : {counters['indexed']}")
    print(f"  Skipped : {counters['skipped']} (unchanged)")
    print(f"  Errors  : {counters['errors']}")

    if counters["indexed"] > 0:
        print(f"\nPopulating search database...")
        with SearchEngine(project_root) as engine:
            synced = engine.sync_from_specs()
        print(f"  Synced {synced} components into search index.")

        print(f"\nIndex stored in: {project_root / '.rtk-sf'}")
        print("Run `rtk-sf serve` to start the MCP server.")
        print("Run `rtk-sf ui` to generate the architecture map.")

    return 0 if counters["errors"] == 0 else 1


# ---------------------------------------------------------------------------
# Subcommand: watch
# ---------------------------------------------------------------------------


def cmd_watch(args: argparse.Namespace) -> int:
    """Watch for file changes and re-index incrementally."""
    from rtk_sf.watcher import FileWatcher

    project_root = Path(args.project_root).resolve()
    watch_path = Path(args.path).resolve() if args.path else None

    watcher = FileWatcher(project_root, watch_path)
    watcher.start(blocking=True)
    return 0


# ---------------------------------------------------------------------------
# Subcommand: serve
# ---------------------------------------------------------------------------


def cmd_serve(args: argparse.Namespace) -> int:
    """Start the MCP stdio JSON-RPC server."""
    from rtk_sf.mcp_server import MCPServer

    project_root = Path(args.project_root).resolve()
    server = MCPServer(project_root)
    server.serve()
    return 0


# ---------------------------------------------------------------------------
# Subcommand: ui
# ---------------------------------------------------------------------------


def cmd_ui(args: argparse.Namespace) -> int:
    """Generate the architecture map HTML file."""
    from rtk_sf.ui_generator import generate_html

    project_root = Path(args.project_root).resolve()
    output = Path(args.output) if args.output else None

    out_path = generate_html(project_root, output)
    print(f"Architecture map generated: {out_path}")
    print("Open the file in your browser to explore the component graph.")
    return 0


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rtk-sf",
        description="Zero-Token Knowledge & Visual Live-Mapping Layer for Salesforce AI Agents",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  rtk-sf index                        # Index ./force-app
  rtk-sf index --path ./src           # Index custom source path
  rtk-sf watch                        # Watch ./force-app for changes
  rtk-sf serve                        # Start MCP server (for Claude Code)
  rtk-sf ui                           # Generate architecture_map.html
  rtk-sf ui --output ~/Desktop/map.html

MCP integration:
  claude mcp add rtk-sf -- python -m rtk_sf serve

Built by furuCRM Inc. — https://www.furucrm.com
""",
    )
    from rtk_sf import __version__
    parser.add_argument(
        "--version", action="version", version=f"rtk-sf {__version__}"
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable debug logging"
    )
    parser.add_argument(
        "--project-root",
        default=".",
        metavar="DIR",
        help="Salesforce project root directory (default: current directory)",
    )

    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")
    subparsers.required = True

    # index
    p_index = subparsers.add_parser("index", help="Index Salesforce metadata")
    p_index.add_argument(
        "--path",
        metavar="PATH",
        default=None,
        help="Path to force-app (or source) directory (default: ./force-app)",
    )
    p_index.add_argument(
        "--force",
        action="store_true",
        help="Re-index all files, even unchanged ones",
    )
    p_index.set_defaults(func=cmd_index)

    # watch
    p_watch = subparsers.add_parser(
        "watch", help="Watch for file changes and re-index"
    )
    p_watch.add_argument(
        "--path",
        metavar="PATH",
        default=None,
        help="Directory to watch (default: ./force-app)",
    )
    p_watch.set_defaults(func=cmd_watch)

    # serve
    p_serve = subparsers.add_parser("serve", help="Start the MCP stdio server")
    p_serve.set_defaults(func=cmd_serve)

    # ui
    p_ui = subparsers.add_parser(
        "ui", help="Generate architecture map HTML"
    )
    p_ui.add_argument(
        "--output",
        metavar="FILE",
        default=None,
        help="Output HTML path (default: dist/architecture_map.html)",
    )
    p_ui.set_defaults(func=cmd_ui)

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    _setup_logging(args.verbose)

    try:
        exit_code = args.func(args)
        sys.exit(exit_code)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        logging.getLogger(__name__).error("Fatal error: %s", exc, exc_info=True)
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
