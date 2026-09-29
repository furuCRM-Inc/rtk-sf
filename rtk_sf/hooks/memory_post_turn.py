#!/usr/bin/env python3
"""
Stop hook — records the turn's git delta into the living memory.

The design called this a "post-turn hook"; Claude Code's actual end-of-turn
event is Stop. Recording the working-tree diff on every turn would log the same
cumulative change repeatedly, so the hook keeps a snapshot of what it last saw
(per-file insertions/deletions plus HEAD) inside `.rtk-sf/history.json` and
records only what moved since. A turn that changed nothing writes nothing.

Claude Code hook protocol:
  stdin : JSON {"session_id": "...", "cwd": "...", "stop_hook_active": bool}
  exit 0: always — a memory failure must never interrupt the session.

Writing an event also triggers the roll-up cascade, so the file compacts itself.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_SNAPSHOT_KEY = "snapshot"
_MAX_SUMMARY_FILES = 3
_GIT_TIMEOUT = 15


def _git(root: Path, *args: str) -> str:
    """Run a read-only git command, returning '' on any failure."""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _numstat(root: Path) -> dict[str, tuple[int, int]]:
    """Per-file (insertions, deletions) for the working tree against HEAD."""
    stats: dict[str, tuple[int, int]] = {}
    for line in _git(root, "diff", "--numstat", "HEAD").splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added, removed, path = parts
        try:
            stats[path] = (int(added), int(removed))
        except ValueError:
            # Binary files report '-' for both counts.
            stats[path] = (0, 0)
    return stats


def _delta(
    current: dict[str, tuple[int, int]],
    previous: dict[str, list],
) -> tuple[list[str], int, int]:
    """Files whose diff changed since the last snapshot, and by how much."""
    files: list[str] = []
    insertions = 0
    deletions = 0
    for path, (added, removed) in current.items():
        before = previous.get(path) or [0, 0]
        prev_added, prev_removed = int(before[0]), int(before[1])
        if (added, removed) == (prev_added, prev_removed):
            continue
        files.append(path)
        insertions += max(0, added - prev_added)
        deletions += max(0, removed - prev_removed)
    # A file whose changes were committed or reverted also counts as activity.
    for path in previous:
        if path not in current:
            files.append(path)
    return files, insertions, deletions


def _summarize(commits: list[str], files: list[str]) -> str:
    if commits:
        return "; ".join(commits[:2])
    if files:
        head = ", ".join(files[:_MAX_SUMMARY_FILES])
        extra = f" (+{len(files) - _MAX_SUMMARY_FILES} more)" if len(files) > _MAX_SUMMARY_FILES else ""
        return f"edited {head}{extra}"
    return ""


def main() -> None:
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except Exception:
        sys.exit(0)

    root = Path(data.get("cwd") or ".").resolve()

    try:
        from rtk_sf.hooks._log import append
        from rtk_sf.memory import HistoryManager

        if not _git(root, "rev-parse", "--is-inside-work-tree"):
            append("memory_post", "skip:not_a_repo", project=root.name)
            sys.exit(0)

        manager = HistoryManager(root)
        store = manager.store
        snapshot = store.get(_SNAPSHOT_KEY) or {}
        previous_files = snapshot.get("files") or {}
        previous_head = snapshot.get("head") or ""

        head = _git(root, "rev-parse", "HEAD")
        current = _numstat(root)
        files, insertions, deletions = _delta(current, previous_files)

        commits: list[str] = []
        if head and previous_head and head != previous_head:
            log = _git(root, "log", "--format=%h %s", f"{previous_head}..{head}")
            commits = [line for line in log.splitlines() if line.strip()]

        # Always refresh the snapshot, even when nothing is recorded, so the next
        # turn compares against current reality.
        store[_SNAPSHOT_KEY] = {
            "head": head,
            "files": {path: list(stat) for path, stat in current.items()},
        }

        if not files and not commits:
            manager.save()
            append("memory_post", "skip:no_change")
            sys.exit(0)

        summary = _summarize(commits, files)
        manager.record_turn(
            summary=summary,
            files=files,
            insertions=insertions,
            deletions=deletions,
            commit=commits[0].split(" ")[0] if commits else None,
            session_id=data.get("session_id"),
        )
        append(
            "memory_post",
            "ok",
            files=len(files),
            insertions=insertions,
            deletions=deletions,
            commits=len(commits),
        )
    except Exception as exc:
        try:
            from rtk_sf.hooks._log import append

            append("memory_post", "error", error=str(exc)[:200])
        except Exception:
            pass

    sys.exit(0)


if __name__ == "__main__":
    main()
