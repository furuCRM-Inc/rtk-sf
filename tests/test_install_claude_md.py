"""
Pins the CLAUDE.md downgrade reported in issue #35.

`rtk-sf install` rewrote a project's CLAUDE.md from a template hardcoded at
v0.5.1 — 14 tools out of 31 — because its "already current" check was a prose
marker ("Image / screenshot rule") last updated in that release. Any newer file
failed the check, so install concluded it was *older* and overwrote it, silently
deleting the guidance for nl_to_soql, get_record_types, get_lwc_targets,
export_system_documentation and get_project_timeline.
"""

from __future__ import annotations

import contextlib
import io

import pytest

from rtk_sf import __version__
from rtk_sf.__main__ import (
    _build_tool_table,
    _patch_claude_md,
    _render_claude_md_block,
    _stamped_version,
    _version_tuple,
)
from rtk_sf.mcp_server import _TOOLS


def _quiet(fn, *args):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(*args)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# The table cannot go stale
# ---------------------------------------------------------------------------

def test_every_registered_tool_appears_in_the_table():
    """The root cause of #35: a hardcoded table listing 14 of 31 tools.

    Generating it from the registry makes the drift impossible, and this test
    is what keeps it that way when a tool is added.
    """
    table = _build_tool_table()
    missing = [t["name"] for t in _TOOLS if t["name"] not in table]
    assert not missing, f"tools absent from the CLAUDE.md table: {missing}"


def test_table_row_count_matches_the_registry():
    assert len([r for r in _build_tool_table().splitlines() if r.strip()]) == len(_TOOLS)


def test_block_is_version_stamped():
    assert _stamped_version(_render_claude_md_block()) == _version_tuple(__version__)


# ---------------------------------------------------------------------------
# Version comparison
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "lower,higher",
    [("0.5.1", "0.11.0"), ("0.9.0", "0.10.0"), ("0.10.3", "0.11.0"), ("1.0.0", "1.0.1")],
)
def test_version_ordering(lower, higher):
    """0.11.0 must sort above 0.5.1 — string comparison gets this wrong."""
    assert _version_tuple(lower) < _version_tuple(higher)


def test_unparsable_version_sorts_lowest():
    assert _version_tuple("not-a-version") == (0,)


# ---------------------------------------------------------------------------
# Writing behaviour
# ---------------------------------------------------------------------------

def test_creates_the_file_when_absent(tmp_path):
    _quiet(_patch_claude_md, tmp_path)
    text = (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")
    assert "## Code Search" in text
    assert _stamped_version(text) == _version_tuple(__version__)


def test_a_newer_file_is_never_overwritten(tmp_path):
    """The #35 failure mode, inverted: an older install must not downgrade a
    file written by a newer one."""
    original = "# Mine\n<!-- rtk-sf:begin 99.0.0 -->\nFUTURE GUIDANCE\n<!-- rtk-sf:end -->\n"
    (tmp_path / "CLAUDE.md").write_text(original, encoding="utf-8")

    out = _quiet(_patch_claude_md, tmp_path)

    assert (tmp_path / "CLAUDE.md").read_text(encoding="utf-8") == original
    assert "newer than this install" in out
    assert not (tmp_path / "CLAUDE.md.rtk-bak").exists()


def test_rewrite_is_idempotent(tmp_path):
    _quiet(_patch_claude_md, tmp_path)
    first = (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")

    out = _quiet(_patch_claude_md, tmp_path)

    assert (tmp_path / "CLAUDE.md").read_text(encoding="utf-8") == first
    assert "already current" in out


def test_legacy_block_is_upgraded_without_losing_newer_tools(tmp_path):
    """The exact regression: an unstamped v0.10-era block is replaced, and the
    tools it documented must still be documented afterwards."""
    legacy = (
        "# Salesforce Project\n\n"
        "## Code Search & Data — Use rtk-sf First (Required)\n\n"
        "| Task | Tool to call |\n|---|---|\n"
        "| Answer a data question | `nl_to_soql(user_input)` |\n"
        "| Read RecordTypes | `get_record_types(object_name)` |\n\n"
        "## Our Own Conventions\n\n"
        "Never deploy on a Friday.\n"
    )
    (tmp_path / "CLAUDE.md").write_text(legacy, encoding="utf-8")

    _quiet(_patch_claude_md, tmp_path)
    text = (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")

    for tool in ("nl_to_soql", "get_record_types", "get_lwc_targets",
                 "export_system_documentation", "get_project_timeline"):
        assert tool in text, f"{tool} was lost — this is issue #35"


def test_surrounding_content_and_order_are_preserved(tmp_path):
    """The old implementation re-inserted the block after line 1, reordering
    the document. The user's own sections must stay where they are."""
    legacy = (
        "# Salesforce Project\n\n"
        "## Our Own Conventions\n\nNever deploy on a Friday.\n\n"
        "## Code Search & Data — Use rtk-sf First (Required)\n\nold block\n\n"
        "## Deployment Notes\n\nSandbox first.\n"
    )
    (tmp_path / "CLAUDE.md").write_text(legacy, encoding="utf-8")

    _quiet(_patch_claude_md, tmp_path)
    text = (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")

    assert "Never deploy on a Friday." in text
    assert "Sandbox first." in text
    assert text.index("Our Own Conventions") < text.index("Code Search")
    assert text.index("Code Search") < text.index("Deployment Notes")
    assert text.splitlines()[0] == "# Salesforce Project"


def test_a_backup_is_written_before_any_rewrite(tmp_path):
    legacy = "# Salesforce Project\n\n## Code Search\n\nold\n"
    (tmp_path / "CLAUDE.md").write_text(legacy, encoding="utf-8")

    _quiet(_patch_claude_md, tmp_path)

    assert (tmp_path / "CLAUDE.md.rtk-bak").read_text(encoding="utf-8") == legacy


def test_file_without_an_rtk_block_is_appended_to_not_displaced(tmp_path):
    other = "# Some Other Project\n\nUnrelated instructions.\n"
    (tmp_path / "CLAUDE.md").write_text(other, encoding="utf-8")

    _quiet(_patch_claude_md, tmp_path)
    text = (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")

    assert text.startswith("# Some Other Project")
    assert "Unrelated instructions." in text
    assert "## Code Search" in text


# ---------------------------------------------------------------------------
# Hook command portability
# ---------------------------------------------------------------------------

def test_hook_command_is_portable_when_python3_can_import_rtk_sf():
    """`.claude/settings.json` is commonly committed, so an absolute path from
    one machine breaks the rest of the team (#35)."""
    import rtk_sf.__main__ as m

    saved = m._PYTHON3_OK
    try:
        m._PYTHON3_OK = True
        assert m._make_hook_cmd("rtk_sf.hooks.ocr_intercept") == (
            "python3 -m rtk_sf.hooks.ocr_intercept"
        )
    finally:
        m._PYTHON3_OK = saved


def test_hook_command_falls_back_to_absolute_path_in_a_venv():
    """When a bare python3 cannot import rtk_sf, the absolute path is required
    and correct."""
    import sys

    import rtk_sf.__main__ as m

    saved = m._PYTHON3_OK
    try:
        m._PYTHON3_OK = False
        assert m._make_hook_cmd("rtk_sf.hooks.ocr_intercept") == (
            f"{sys.executable} -m rtk_sf.hooks.ocr_intercept"
        )
    finally:
        m._PYTHON3_OK = saved
