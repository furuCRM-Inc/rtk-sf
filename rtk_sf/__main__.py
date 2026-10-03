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

# CLAUDE.md block delimiters.
#
# The block is bounded by HTML comments carrying the version that wrote it, so
# install can compare versions instead of guessing from prose. The previous
# scheme keyed off a phrase ("Image / screenshot rule") that was last updated
# in v0.5.1: any newer CLAUDE.md failed the check, so install concluded the file
# was *older* and overwrote it with the v0.5.1 template — a silent downgrade
# (#35). A version stamp can express "newer than me"; a phrase cannot.
_BLOCK_BEGIN = "<!-- rtk-sf:begin "
_BLOCK_END = "<!-- rtk-sf:end -->"
_MARKER_BASE = "## Code Search"  # legacy, unstamped blocks from <= v0.11.0

# Task phrasing for the tools worth calling out by use case. Tools absent from
# this map still reach the table — the row is derived from the tool's own
# description — so a newly registered tool can never silently go missing.
_TOOL_TASKS: dict[str, str] = {
    "search_codebase": "Find a component by name or keyword",
    "query_compressed_spec": "Read a component spec / fields / methods",
    "get_relations": "Blast-radius before editing",
    "list_components": "List all Apex classes / objects / flows",
    "annotate_component": "Write discovered business logic back",
    "get_class_skeleton": "Read an Apex class before editing (surgical)",
    "sf_command": "Deploy / retrieve / run tests silently",
    "get_object_schema": "Get object field list for data creation",
    "soql_query": "Inspect existing records (sample only)",
    "nl_to_soql": "Answer a natural-language data question directly",
    "get_record_types": "Read RecordType definitions for an object",
    "get_lwc_targets": "List LWC components and their targets",
    "export_system_documentation": "Generate system docs / diagrams (to disk)",
    "get_project_timeline": "Ask what has been worked on recently",
    "compact_prompt": "Compact a bilingual prompt before sending",
    "validate_apex": "Dry-run Apex code before deploy",
    "validate_soql": "Dry-run SOQL before executing",
    "extract_image_text": "Extract text from a screenshot/image",
    "get_roi_stats": "View token/dollar savings this session",
    "read_data_file": "Preview a CSV / JSON / JSONL file",
    "hybrid_plan": "Route Apex methods between a local worker and yourself",
    "hybrid_delegate": "Run method-scoped Apex edits on the local worker",
    "hybrid_review": "Record a review finding for the local worker",
    "get_java_skeleton": "Read a Java file (structural)",
    "run_java_build": "Run a Maven / Gradle build",
    "get_kotlin_skeleton": "Read a Kotlin file (structural)",
    "run_gradle": "Run a Gradle task",
    "get_ts_skeleton": "Read a TypeScript / JS file (structural)",
    "run_js_tests": "Run Jest / Vitest / Playwright tests",
    "get_python_skeleton": "Read a Python file (structural)",
    "run_python_tests": "Run pytest",
}


def _version_tuple(text: str) -> tuple[int, ...]:
    """Parse a dotted version into a comparable tuple; unparsable -> (0,)."""
    parts: list[int] = []
    for chunk in text.strip().split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts) or (0,)


def _stamped_version(text: str) -> tuple[int, ...] | None:
    """Return the version stamped on an existing rtk-sf block, if any."""
    start = text.find(_BLOCK_BEGIN)
    if start == -1:
        return None
    end = text.find("-->", start)
    if end == -1:
        return None
    return _version_tuple(text[start + len(_BLOCK_BEGIN) : end])


def _build_tool_table() -> str:
    """Render the tool table from the live registry.

    Generated rather than hardcoded so it cannot drift from the tools the
    package actually serves — the failure in #35, where a literal table listed
    14 of 31 tools. Row order follows `_TOOL_TASKS` for the curated entries,
    then registry order for anything new.
    """
    from rtk_sf.mcp_server import _TOOLS

    registered = {t["name"]: t for t in _TOOLS}
    rows: list[str] = []
    seen: set[str] = set()

    def _row(name: str, task: str) -> str:
        tool = registered[name]
        args = tool.get("inputSchema", {}).get("required", [])
        call = f"{name}({', '.join(args)})" if args else f"{name}()"
        return f"| {task} | `{call}` |"

    for name, task in _TOOL_TASKS.items():
        if name in registered:
            rows.append(_row(name, task))
            seen.add(name)

    for name in registered:
        if name in seen:
            continue
        # Fall back to the first sentence of the tool's own description.
        desc = " ".join(registered[name]["description"].split())
        task = desc.split(". ")[0].rstrip(".")
        rows.append(_row(name, task[:70]))

    return "\n".join(rows)


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


def _render_claude_md_block() -> str:
    """The rtk-sf guidance block, stamped with the version that wrote it."""
    from rtk_sf import __version__

    return f"""{_BLOCK_BEGIN}{__version__} -->
## Code Search & Data — Use rtk-sf First (Required)

This project is indexed by **rtk-sf**. Always use the MCP tools before reading raw files or calling sf CLI:

| Task | Tool to call |
|---|---|
{_build_tool_table()}

**Never** do these directly — use the tool instead:
- Read a raw .cls file                      -> `get_class_skeleton`
- sf sobject describe                       -> `get_object_schema`
- sf data query                             -> `soql_query`
- sf project deploy start                   -> `sf_command(action="deploy")`
- Any pipeline scan over recordTypes/, fields/, or layouts/ -> `get_record_types` or `get_object_schema`
- grep/cat/find against lwc/*/*.js-meta.xml -> `get_lwc_targets()`
- Hand-writing architecture docs or diagrams -> `export_system_documentation`
- Reading git log to reconstruct recent work -> `get_project_timeline`
- Read an inline pasted image with native vision -> ask for the file path, then `extract_image_text(path)`

**Image / screenshot rule:** `extract_image_text` requires a file path on disk.
If the user pastes an image inline without a path, reply:
"To save vision tokens, please share the file path (e.g. `~/Downloads/screenshot.png`) so I can run local OCR instead."

For natural-language data questions, try `nl_to_soql` **before** reaching for
`get_object_schema` plus a hand-written `soql_query` — it is a deterministic
compiler with no model call inside it. Fall back to the manual path only when it
returns `{{"intent": "UNKNOWN"}}`. `RECORD_UPDATE` results are proposals only —
never execute them as DML without confirming with the user first.

If search returns no results, re-index with: `python3 -m rtk_sf index`
Do NOT use `npx rtk-sf` — rtk-sf is a Python package, not npm.
{_BLOCK_END}"""


def _patch_claude_md(project_root: Path) -> None:
    """Insert or refresh the rtk-sf block in CLAUDE.md.

    Three rules, all of them consequences of #35:

    * **Never write older content over newer.** The block carries the version
      that wrote it; if the file's stamp is at or above the running version, the
      file is left untouched. A user on a newer rtk-sf cannot be downgraded by
      an older one.
    * **Never move the user's content.** The block is replaced where it already
      sits. The previous implementation re-inserted it after line 1, reordering
      the document.
    * **Always keep a copy.** `CLAUDE.md.rtk-bak` is written before any change,
      because this file is hand-maintained guidance, not a generated artifact.
    """
    from rtk_sf import __version__

    claude_md = project_root / "CLAUDE.md"
    block = _render_claude_md_block()
    current = _version_tuple(__version__)

    if not claude_md.exists():
        claude_md.write_text(f"# Salesforce Project\n\n{block}\n", encoding="utf-8")
        _success("CLAUDE.md created with rtk-sf tool instructions.")
        return

    text = claude_md.read_text(encoding="utf-8")
    stamped = _stamped_version(text)

    if stamped is not None and stamped >= current:
        if stamped > current:
            _warn(
                f"CLAUDE.md was written by rtk-sf {'.'.join(map(str, stamped))}, "
                f"newer than this install ({__version__}). Leaving it alone."
            )
        else:
            _success(f"CLAUDE.md already current (rtk-sf {__version__}). Skipping.")
        return

    backup = claude_md.with_suffix(".md.rtk-bak")
    backup.write_text(text, encoding="utf-8")

    if stamped is not None:
        # Stamped block: replace exactly that span, leaving everything else.
        begin = text.index(_BLOCK_BEGIN)
        close = text.find(_BLOCK_END)
        if close == -1:
            updated = text[:begin] + block
        else:
            updated = text[:begin] + block + text[close + len(_BLOCK_END) :]
        _warn(
            f"Upgrading CLAUDE.md block from rtk-sf "
            f"{'.'.join(map(str, stamped))} to {__version__}..."
        )
    elif _MARKER_BASE in text:
        # Legacy unstamped block: replace from its heading to the next heading,
        # in place. Content before and after is preserved verbatim.
        lines = text.splitlines(keepends=True)
        begin_idx = next(i for i, ln in enumerate(lines) if ln.startswith(_MARKER_BASE))
        end_idx = len(lines)
        for i in range(begin_idx + 1, len(lines)):
            if lines[i].startswith("## "):
                end_idx = i
                break
        updated = "".join(lines[:begin_idx]) + block + "\n\n" + "".join(lines[end_idx:])
        _warn(f"Upgrading unstamped CLAUDE.md block to rtk-sf {__version__}...")
    else:
        # No rtk-sf block at all — append rather than displace the first line.
        updated = text.rstrip("\n") + f"\n\n{block}\n"
        _warn("Adding the rtk-sf block to CLAUDE.md...")

    claude_md.write_text(updated, encoding="utf-8")
    _success(
        f"CLAUDE.md updated with rtk-sf {__version__} tool instructions "
        f"(previous version saved as {backup.name})."
    )


_OCR_MODULE = "rtk_sf.hooks.ocr_intercept"
_COMPACT_MODULE = "rtk_sf.hooks.compact_prompt"
_MEMORY_PRE_MODULE = "rtk_sf.hooks.memory_pre_turn"
_MEMORY_POST_MODULE = "rtk_sf.hooks.memory_post_turn"


def _make_hook_cmd(module: str) -> str:
    """Build the hook command, preferring a portable `python3` invocation.

    `.claude/settings.json` is commonly committed and shared across a team, so
    baking in `sys.executable` writes one developer's interpreter path into
    everyone's config. Worse, it is whichever interpreter happened to run
    install — observed in #35 as an unrelated ESP-IDF environment that was
    merely first on PATH.

    So: use plain `python3` when that interpreter can actually import rtk_sf,
    and fall back to the absolute path only when it cannot (a venv or a
    non-default interpreter), where the absolute path is genuinely required.
    """
    if _python3_has_rtk_sf():
        return f"python3 -m {module}"
    return f"{sys.executable} -m {module}"


def _python3_has_rtk_sf() -> bool:
    """True when a bare `python3` can import rtk_sf (cached per process)."""
    global _PYTHON3_OK
    if _PYTHON3_OK is None:
        import shutil
        import subprocess

        exe = shutil.which("python3")
        if exe is None:
            _PYTHON3_OK = False
        elif Path(exe).resolve() == Path(sys.executable).resolve():
            _PYTHON3_OK = True  # same interpreter; no need to spawn
        else:
            try:
                _PYTHON3_OK = (
                    subprocess.run(
                        [exe, "-c", "import rtk_sf"],
                        capture_output=True,
                        timeout=15,
                    ).returncode
                    == 0
                )
            except (OSError, subprocess.SubprocessError):
                _PYTHON3_OK = False
    return _PYTHON3_OK


_PYTHON3_OK: bool | None = None


def _patch_claude_settings(project_root: Path) -> None:
    """Wire rtk-sf hooks into .claude/settings.json (merge, never overwrite).

    Uses sys.executable so the hook always runs with the Python environment
    that has rtk_sf installed — not the system python3 which may differ.
    Re-running install updates the path if the Python executable changed.
    """
    import json as _json

    ocr_cmd = _make_hook_cmd(_OCR_MODULE)
    compact_cmd = _make_hook_cmd(_COMPACT_MODULE)
    memory_pre_cmd = _make_hook_cmd(_MEMORY_PRE_MODULE)
    memory_post_cmd = _make_hook_cmd(_MEMORY_POST_MODULE)

    settings_path = project_root / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    config: dict = {}
    if settings_path.exists():
        try:
            config = _json.loads(settings_path.read_text(encoding="utf-8"))
        except Exception:
            config = {}

    hooks = config.setdefault("hooks", {})

    def _hook_key(module: str) -> str:
        """Bare script name shared by all formats of an rtk_sf hook command."""
        return module.split(".")[-1]  # e.g. "ocr_intercept"

    def _find_and_dedupe(event: str, module: str, correct_cmd: str) -> tuple[dict | None, bool]:
        """Find the rtk_sf hook entry for *module*, update its command, and
        remove any duplicate outer entries.  Returns (hook_dict_or_None, changed).
        """
        key = _hook_key(module)
        first_hook: dict | None = None
        dirty = False

        surviving_entries = []
        for entry in hooks.get(event, []):
            matches = [h for h in entry.get("hooks", []) if key in h.get("command", "")]
            if not matches:
                surviving_entries.append(entry)
                continue
            if first_hook is None:
                # keep this entry; update its command if stale
                h = matches[0]
                if h.get("command") != correct_cmd:
                    h["command"] = correct_cmd
                    dirty = True
                first_hook = h
                surviving_entries.append(entry)
            else:
                dirty = True  # drop duplicate outer entry

        if dirty or len(surviving_entries) != len(hooks.get(event, [])):
            hooks[event] = surviving_entries
            dirty = True
        return first_hook, dirty

    changed = False

    # PreToolUse[Read] → OCR intercept
    existing_ocr, ocr_changed = _find_and_dedupe("PreToolUse", _OCR_MODULE, ocr_cmd)
    changed |= ocr_changed
    if existing_ocr is None:
        hooks.setdefault("PreToolUse", []).append({
            "matcher": "Read",
            "hooks": [{"type": "command", "command": ocr_cmd}],
        })
        changed = True

    # UserPromptSubmit → compact prompt
    existing_compact, compact_changed = _find_and_dedupe("UserPromptSubmit", _COMPACT_MODULE, compact_cmd)
    changed |= compact_changed
    if existing_compact is None:
        hooks.setdefault("UserPromptSubmit", []).append({
            "matcher": "",
            "hooks": [{"type": "command", "command": compact_cmd}],
        })
        changed = True

    # UserPromptSubmit → living-memory digest.
    # A second UserPromptSubmit hook is safe alongside the compactor: this one
    # returns additionalContext rather than rewriting the prompt.
    existing_mem_pre, mem_pre_changed = _find_and_dedupe(
        "UserPromptSubmit", _MEMORY_PRE_MODULE, memory_pre_cmd
    )
    changed |= mem_pre_changed
    if existing_mem_pre is None:
        hooks.setdefault("UserPromptSubmit", []).append({
            "matcher": "",
            "hooks": [{"type": "command", "command": memory_pre_cmd}],
        })
        changed = True

    # Stop → record the turn's git delta into the living memory
    existing_mem_post, mem_post_changed = _find_and_dedupe(
        "Stop", _MEMORY_POST_MODULE, memory_post_cmd
    )
    changed |= mem_post_changed
    if existing_mem_post is None:
        hooks.setdefault("Stop", []).append({
            "matcher": "",
            "hooks": [{"type": "command", "command": memory_post_cmd}],
        })
        changed = True

    if changed:
        # Trailing newline: the file is commonly committed, and dropping it
        # shows up as a spurious "\ No newline at end of file" in every diff.
        settings_path.write_text(
            _json.dumps(config, indent=2) + "\n", encoding="utf-8"
        )
        _success(
            ".claude/settings.json updated with rtk-sf hooks "
            "(OCR intercept, prompt compactor, living memory)."
        )
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


def cmd_docs(args: argparse.Namespace) -> int:
    """Generate system documentation and diagrams to disk."""
    from rtk_sf.docgen import export_documentation

    project_root = Path(args.project_root).resolve()
    result = export_documentation(
        doc_type=args.doc_type,
        output_dir=args.output_dir,
        project_root=project_root,
    )
    print(result)
    return 1 if result.startswith("\u274c") else 0


def cmd_timeline(args: argparse.Namespace) -> int:
    """Print the living-memory project timeline for one scope."""
    import json as _json

    from rtk_sf.memory import HistoryManager

    manager = HistoryManager(Path(args.project_root).resolve())
    if not manager.path.exists():
        _warn("No project history yet.")
        _info("Record turns by installing the Stop hook: python3 -m rtk_sf.hooks.memory_post_turn")
        return 0
    try:
        data = manager.timeline(args.scope)
    except ValueError as exc:
        _warn(str(exc))
        return 1
    manager.save()
    print(_json.dumps(data, ensure_ascii=False, indent=2))
    return 0


def cmd_hook_stats(args: argparse.Namespace) -> int:
    """Show recent hook activity from ~/.rtk-sf-hooks.log."""
    from pathlib import Path as _Path
    log_path = _Path.home() / ".rtk-sf-hooks.log"
    if not log_path.exists():
        _warn("No hook log yet — hooks have not fired since last install.")
        _info("Tip: hooks only fire inside a Claude Code session.")
        return 0

    from rtk_sf.hooks._log import tail
    n = getattr(args, "lines", 20)
    records = tail(n)
    if not records:
        _warn("Log exists but is empty.")
        return 0

    import json as _json
    # Summary counters
    stats: dict[str, dict[str, int]] = {}
    for r in records:
        hook = r.get("hook", "?")
        status = r.get("status", "?")
        stats.setdefault(hook, {}).setdefault(status, 0)
        stats[hook][status] += 1

    _bold("\n━━━  rtk-sf hook activity (last %d entries)  ━━━" % len(records))
    print()
    for hook, counts in stats.items():
        label = {"compact": "NLP compact_prompt", "ocr": "OCR intercept"}.get(hook, hook)
        print(f"  {label}:")
        for status, count in counts.items():
            print(f"    {status:30s}  ×{count}")
    print()
    _bold("Recent entries:")
    for r in records[-10:]:
        ts = r.get("ts", "")[:19].replace("T", " ")
        hook = r.get("hook", "?")
        status = r.get("status", "?")
        extra = {k: v for k, v in r.items() if k not in ("ts", "hook", "status")}
        extra_str = ("  " + _json.dumps(extra, ensure_ascii=False)) if extra else ""
        print(f"  {ts}  [{hook:7s}]  {status}{extra_str}")
    print()
    _info(f"Full log: {log_path}")
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
  rtk-sf docs                         # Generate the full document set to docs/
  rtk-sf docs sequence_diagrams       # One document only
  rtk-sf timeline last_7_days         # Living-memory weekly time series
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

    # docs
    p_docs = subparsers.add_parser(
        "docs", help="Generate system documentation and diagrams to disk"
    )
    p_docs.add_argument(
        "doc_type",
        nargs="?",
        default="all",
        choices=[
            "function_matrix",
            "function_usecases",
            "sequence_diagrams",
            "business_scenarios",
            "object_definitions",
            "metadata_inventory",
            "screen_list",
            "erd",
            "system_doc",
            "all",
        ],
        help="Document to generate (default: all)",
    )
    p_docs.add_argument(
        "--output-dir",
        metavar="DIR",
        default="docs",
        help="Destination directory (default: docs)",
    )
    p_docs.set_defaults(func=cmd_docs)

    # timeline
    p_timeline = subparsers.add_parser(
        "timeline", help="Print the living-memory project timeline as JSON"
    )
    p_timeline.add_argument(
        "scope",
        nargs="?",
        default="recent_3_days",
        choices=[
            "recent_3_days",
            "last_7_days",
            "current_month",
            "fiscal_quarters",
            "fiscal_years",
            "all",
        ],
        help="Time bucket to read (default: recent_3_days)",
    )
    p_timeline.set_defaults(func=cmd_timeline)

    # hook-stats
    p_stats = subparsers.add_parser(
        "hook-stats",
        help="Show recent OCR / NLP hook activity from ~/.rtk-sf-hooks.log",
    )
    p_stats.add_argument(
        "-n", "--lines",
        type=int,
        default=20,
        metavar="N",
        help="Number of log entries to display (default: 20)",
    )
    p_stats.set_defaults(func=cmd_hook_stats)

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    # Pre-parse intercept: handle 'update' before argparse sees it.
    # This lets older installed versions (that don't have 'update' registered)
    # still self-upgrade via `python3 -m rtk_sf update`.
    if len(sys.argv) >= 2 and sys.argv[1] == "update":
        # The intercept parser sets add_help=False so it can tolerate unknown
        # flags, which also means it swallows -h/--help — so `update --help`
        # used to *perform* the upgrade and re-run install. A help flag must
        # never have side effects (#35), so it is handled before anything runs.
        if any(flag in sys.argv[2:] for flag in ("-h", "--help")):
            print(
                "usage: rtk-sf update [-h] [--project-root DIR]\n\n"
                "Upgrade rtk-sf to the latest main, then re-run install\n"
                "(index the project, refresh the CLAUDE.md block, wire hooks).\n\n"
                "options:\n"
                "  -h, --help          show this help message and exit\n"
                "  --project-root DIR  project to set up after upgrading (default: .)\n"
            )
            sys.exit(0)
        import argparse as _ap
        _p = _ap.ArgumentParser(add_help=False)
        _p.add_argument("--project-root", default=".")
        _p.add_argument("update")
        _known, _ = _p.parse_known_args(sys.argv[1:])
        _ns = _ap.Namespace(project_root=_known.project_root, verbose=False)
        sys.exit(cmd_update(_ns))

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
