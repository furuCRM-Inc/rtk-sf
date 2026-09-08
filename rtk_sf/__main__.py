"""
__main__.py — CLI entry point for rtk-sf.

Supports subcommands:
    install — Full one-command setup: index + patch CLAUDE.md + print next steps
    index   — Index a Salesforce DX project
    watch   — Watch for file changes and re-index incrementally
    serve   — Start the MCP stdio JSON-RPC server
    ui      — Generate the architecture map HTML

Usage:
    python3 -m rtk_sf install            # run after pip install
    python3 -m rtk_sf <subcommand> [options]
    rtk-sf <subcommand> [options]       (when installed via pip)
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
from pathlib import Path

# CLAUDE.md markers used by install and upgrade detection
_MARKER_V5_1 = "Image / screenshot rule"  # v0.5.1 — inline image guidance added
_MARKER_V5 = "get_roi_stats"              # v0.5 — 14-tool block, missing image rule
_MARKER_V4 = "get_class_skeleton"         # v0.4 — 9-tool block
_MARKER_BASE = "## Code Search"


def _info(msg: str) -> None:
    print(f"\033[34m[rtk-sf]\033[0m {msg}")

def _success(msg: str) -> None:
    print(f"\033[32m[rtk-sf]\033[0m {msg}")

def _warn(msg: str) -> None:
    print(f"\033[33m[rtk-sf]\033[0m {msg}")

def _bold(msg: str) -> None:
    print(f"\033[1m{msg}\033[0m")


def _detect_sf_source(project_root: Path) -> Path | None:
    if (project_root / "force-app").is_dir():
        return project_root / "force-app"
    src = project_root / "src"
    if src.is_dir() and any(src.rglob("*.cls")):
        return src
    if any(project_root.rglob("*.cls")):
        return project_root
    return None


def _patch_claude_md(project_root: Path) -> None:
    claude_md = project_root / "CLAUDE.md"

    if claude_md.exists() and _MARKER_V5_1 in claude_md.read_text(encoding="utf-8"):
        _success("CLAUDE.md already has rtk-sf v0.5.1 instructions. Skipping.")
        return

    # Strip old block (v0.3 / v0.4 / v0.5) and rewrite with latest tool list
    if claude_md.exists() and _MARKER_BASE in claude_md.read_text(encoding="utf-8"):
        text = claude_md.read_text(encoding="utf-8")
        if _MARKER_V5 in text and _MARKER_V4 in text:
            _warn("Upgrading CLAUDE.md from v0.5 to v0.5.1 (inline image guidance)...")
        elif _MARKER_V4 in text:
            _warn("Upgrading CLAUDE.md from v0.4 to v0.5.1 (5 new tools + image rule)...")
        else:
            _warn("Upgrading CLAUDE.md from v0.3 to v0.5.1...")
        lines = claude_md.read_text(encoding="utf-8").splitlines()
        in_block = False
        kept: list[str] = []
        for line in lines:
            if line.startswith(_MARKER_BASE):
                in_block = True
            elif in_block and line.startswith("## "):
                in_block = False
            if not in_block:
                kept.append(line)
        claude_md.write_text("\n".join(kept), encoding="utf-8")

    block = """
## Code Search & Data — Use rtk-sf First (Required)

This project is indexed by **rtk-sf**. Always use the MCP tools before reading raw files or calling sf CLI:

| Task | Tool to call |
|---|---|
| Find a component by name or keyword | `search_codebase(query)` |
| Read a component spec / fields / methods | `query_compressed_spec(component_name)` |
| Blast-radius before editing | `get_relations(component_name)` |
| List all Apex classes / objects / flows | `list_components(type)` |
| Write discovered business logic back | `annotate_component(component_name, key, value)` |
| Read an Apex class before editing (surgical) | `get_class_skeleton(component_name, focus_methods)` |
| Deploy / retrieve / run tests silently | `sf_command(action, target_org, ...)` |
| Get object field list for data creation | `get_object_schema(object_name)` |
| Inspect existing records (sample only) | `soql_query(query, target_org, sample_size)` |
| Compact a bilingual prompt before sending | `compact_prompt(text)` |
| Dry-run Apex code before deploy | `validate_apex(code)` |
| Dry-run SOQL before executing | `validate_soql(query)` |
| Extract text from a screenshot/image | `extract_image_text(image_path)` |
| View token/dollar savings this session | `get_roi_stats()` |

**Never** do these directly — use the tool instead:
- Read a raw .cls file       → `get_class_skeleton`
- sf sobject describe        → `get_object_schema`
- sf data query              → `soql_query`
- sf project deploy start    → `sf_command(action="deploy")`
- Read an inline pasted image with native vision → ask for the file path, then `extract_image_text(path)`

**Image / screenshot rule:** `extract_image_text` requires a file path on disk.
If the user pastes an image inline without a path, reply:
"To save vision tokens, please share the file path (e.g. `~/Downloads/screenshot.png`) so I can run local OCR instead."

If search returns no results, re-index with: `python3 -m rtk_sf index`
Do NOT use `npx rtk-sf` — rtk-sf is a Python package, not npm.
"""

    if claude_md.exists():
        original = claude_md.read_text(encoding="utf-8")
        first_line, _, rest = original.partition("\n")
        claude_md.write_text(f"{first_line}\n{block}\n{rest}", encoding="utf-8")
        _success("CLAUDE.md updated with rtk-sf tool instructions.")
    else:
        claude_md.write_text(f"# Salesforce Project\n{block}\n", encoding="utf-8")
        _success("CLAUDE.md created with rtk-sf tool instructions.")


_OCR_CMD = "python3 -m rtk_sf.hooks.ocr_intercept"
_COMPACT_CMD = "python3 -m rtk_sf.hooks.compact_prompt"


def _patch_claude_settings(project_root: Path) -> None:
    """Wire rtk-sf hooks into .claude/settings.json (merge, never overwrite).

    Uses `python3 -m rtk_sf.hooks.*` so hooks always resolve to the currently
    installed rtk-sf version — no path updates needed after pip upgrade.
    """
    import json as _json

    settings_path = project_root / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    config: dict = {}
    if settings_path.exists():
        try:
            config = _json.loads(settings_path.read_text(encoding="utf-8"))
        except Exception:
            config = {}

    hooks = config.setdefault("hooks", {})

    def _has_hook(event: str, cmd: str) -> bool:
        for entry in hooks.get(event, []):
            for h in entry.get("hooks", []):
                if h.get("command", "") == cmd:
                    return True
        return False

    changed = False

    # PreToolUse[Read] → OCR intercept
    if not _has_hook("PreToolUse", _OCR_CMD):
        hooks.setdefault("PreToolUse", []).append({
            "matcher": "Read",
            "hooks": [{"type": "command", "command": _OCR_CMD}],
        })
        changed = True

    # UserPromptSubmit → compact prompt
    if not _has_hook("UserPromptSubmit", _COMPACT_CMD):
        hooks.setdefault("UserPromptSubmit", []).append({
            "matcher": "",
            "hooks": [{"type": "command", "command": _COMPACT_CMD}],
        })
        changed = True

    if changed:
        settings_path.write_text(_json.dumps(config, indent=2), encoding="utf-8")
        _success(".claude/settings.json updated with rtk-sf hooks (OCR intercept + prompt compactor).")
    else:
        _success(".claude/settings.json already has rtk-sf hooks. Skipping.")


def _print_next_steps() -> None:
    python_cmd = "python3"

    print()
    _bold("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    _bold("  rtk-sf is ready!")
    _bold("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print()
    _info("Next steps:")
    print()
    print("  1. Register with Claude Code:")
    print(f"     \033[1mclaude mcp add rtk-sf -- {python_cmd} -m rtk_sf serve\033[0m")
    print()
    print("  2. Generate the visual architecture map:")
    print(f"     \033[1m{python_cmd} -m rtk_sf ui\033[0m")
    print(f"     \033[1mopen dist/architecture_map.html\033[0m")
    print()
    print("  3. Enable live file watching during development:")
    print(f"     \033[1m{python_cmd} -m rtk_sf watch\033[0m")
    print()
    print("  4. Re-index after adding new Apex classes or objects:")
    print(f"     \033[1m{python_cmd} -m rtk_sf index\033[0m")
    print()

    if not shutil.which("rtk-sf"):
        import sysconfig
        scripts_dir = sysconfig.get_path("scripts")
        if scripts_dir:
            _warn("'rtk-sf' CLI not in PATH. Add it permanently:")
            print(f'     \033[1mexport PATH="$PATH:{scripts_dir}"\033[0m')
            print()

    _info("Docs: https://github.com/furuCRM-Inc/rtk-sf")
    print()
    print(f"Built with love by \033[1mfuruCRM Inc.\033[0m — https://www.furucrm.com")
    print()


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )


# ---------------------------------------------------------------------------
# Subcommand: install (full setup)
# ---------------------------------------------------------------------------


def cmd_setup(args: argparse.Namespace) -> int:
    """Full one-command setup: detect project, index, patch CLAUDE.md, print next steps."""
    from rtk_sf.indexer import SalesforceIndexer
    from rtk_sf.search import SearchEngine
    from rtk_sf import __version__

    project_root = Path(args.project_root).resolve()

    print()
    _bold("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    _bold(f"  rtk-sf {__version__} — setup")
    _bold("  Zero-Token Knowledge Layer for Salesforce AI Agents")
    _bold("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print()

    # Step 1: detect Salesforce source directory
    sf_source = _detect_sf_source(project_root)
    if sf_source:
        _success(f"Salesforce source detected: {sf_source.relative_to(project_root)}/")
    else:
        _warn("No Apex (.cls) files found. Indexing project root.")
        sf_source = None

    # Step 2: index
    _info("Indexing Salesforce metadata...")
    indexer = SalesforceIndexer(project_root)
    counters = indexer.index_project(sf_source)
    _success(
        f"Index complete — "
        f"{counters['indexed']} indexed, "
        f"{counters['skipped']} skipped, "
        f"{counters['errors']} errors."
    )

    if counters["indexed"] > 0:
        with SearchEngine(project_root) as engine:
            synced = engine.sync_from_specs()
        _success(f"Search index ready: {synced} components.")

    # Step 3: patch CLAUDE.md
    _patch_claude_md(project_root)

    # Step 4: wire hooks into .claude/settings.json
    _patch_claude_settings(project_root)

    # Step 5: next steps
    _print_next_steps()

    return 0 if counters["errors"] == 0 else 1


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
# Subcommand: update
# ---------------------------------------------------------------------------


def cmd_update(args: argparse.Namespace) -> int:
    """Upgrade rtk-sf to latest main, then run install."""
    import subprocess
    from rtk_sf import __version__

    _bold("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    _bold(f"  rtk-sf update  (current: {__version__})")
    _bold("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print()

    repo = "https://github.com/furuCRM-Inc/rtk-sf.git"
    pkg_all = f"rtk-sf[all] @ git+{repo}@main"
    pkg_core = f"git+{repo}@main"

    # Step 1: ensure pip >= 22
    _info("Upgrading pip...")
    r = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--upgrade", "pip", "--quiet"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        _warn(f"pip upgrade warning: {r.stderr.strip()}")

    # Step 2: try rtk-sf[all], fall back to core if heavy deps fail to build
    _info("Downloading latest rtk-sf from GitHub (with OCR + vector extras)...")
    r = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--force-reinstall",
         "--no-cache-dir", "--quiet", pkg_all],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        _warn("Full install failed (likely missing binary wheel for OCR deps).")
        _warn("Falling back to core install (no OCR, no vector re-ranking)...")
        r = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--force-reinstall",
             "--no-cache-dir", "--quiet", pkg_core],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            print(r.stderr, file=sys.stderr)
            print(f"pip install failed. Try manually:\n  pip install \"{pkg_all}\"",
                  file=sys.stderr)
            return 1
        _warn("Core installed. To add OCR later: pip install paddleocr Pillow")

    # Step 3: re-exec install with the freshly installed version
    _info("Running install with new version...")
    r = subprocess.run(
        [sys.executable, "-m", "rtk_sf",
         "--project-root", str(Path(args.project_root).resolve()),
         "install"],
    )
    return r.returncode


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
  rtk-sf install                      # First-time setup
  rtk-sf update                       # Upgrade to latest version
  rtk-sf index                        # Re-index ./force-app
  rtk-sf index --path ./src           # Index custom source path
  rtk-sf watch                        # Watch ./force-app for changes
  rtk-sf serve                        # Start MCP server (for Claude Code)
  rtk-sf ui                           # Generate architecture_map.html
  rtk-sf ui --output ~/Desktop/map.html

MCP integration:
  claude mcp add rtk-sf -- python3 -m rtk_sf serve

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

    # install (full setup)
    p_setup = subparsers.add_parser(
        "install",
        help="Full setup: index project + patch CLAUDE.md + wire hooks",
    )
    p_setup.set_defaults(func=cmd_setup)

    # update
    p_update = subparsers.add_parser(
        "update",
        help="Upgrade rtk-sf to latest version, then re-run install",
    )
    p_update.set_defaults(func=cmd_update)

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
