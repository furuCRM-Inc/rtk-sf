"""
state.py — the agent handover store.

Why this is not kept inside `.rtk-sf/specs/<Component>.yaml`: that file is
*generated*. `Indexer._write_spec` overwrites it wholesale, and `watcher.py`
calls `index_file(force=True)` on every save. A worker that edits a `.cls` file
therefore triggers a reindex that erases anything appended to the spec — the
handover log would be destroyed by the very write it was recording.

So handover state lives in its own tree, `.rtk-sf/handover/<Component>.yaml`,
which nothing regenerates.

Token design. The point of a state file is that an agent reads a *verdict*
instead of re-reading generated code. Two properties make that real:

  * `render_delta(since)` returns only records whose revision is newer than the
    caller's watermark — tens of tokens per turn, not the whole log.
  * Per-method `body_hash` (from complexity.MethodUnit) marks exactly which
    method moved, so an unchanged method is never re-reported.

Note on the "static prefix / dynamic tail" layout: ordering keys inside a file
does not affect Claude's prompt cache (see docs/hybrid-orchestration.md). What
reduces tokens is returning less, which is what `render_delta` does. Key order
here is chosen for human readability only, and section comments are not used —
`yaml.safe_dump` cannot round-trip them, so they would vanish on first write.
"""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path
from typing import Any

import yaml

HANDOVER_DIR = "handover"

# Terminal and non-terminal states a method record can hold.
STATUS_PENDING = "PENDING"
STATUS_DELEGATED = "DELEGATED"
STATUS_COMPLETED = "COMPLETED"
STATUS_REJECTED = "REJECTED"
STATUS_FAILED = "FAILED"
STATUS_NEEDS_CLAUDE = "NEEDS_CLAUDE"

_VALID_STATUSES = frozenset(
    {
        STATUS_PENDING,
        STATUS_DELEGATED,
        STATUS_COMPLETED,
        STATUS_REJECTED,
        STATUS_FAILED,
        STATUS_NEEDS_CLAUDE,
    }
)


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class HandoverState:
    """Read/modify/write the handover record for one component."""

    def __init__(self, rtk_dir: str | Path, component: str) -> None:
        self.rtk_dir = Path(rtk_dir)
        self.component = component
        self.dir = self.rtk_dir / HANDOVER_DIR
        self.path = self.dir / f"{component}.yaml"
        self._data: dict[str, Any] | None = None

    # ------------------------------------------------------------------
    # Load / save
    # ------------------------------------------------------------------

    def _blank(self) -> dict[str, Any]:
        return {
            "component": self.component,
            "revision": 0,
            "updated_at": _utc_now(),
            "source_file": "",
            "methods": {},
        }

    @property
    def data(self) -> dict[str, Any]:
        if self._data is None:
            self._data = self.load()
        return self._data

    def load(self) -> dict[str, Any]:
        """Load the record, tolerating a missing, empty, or corrupt file.

        A handover log is an optimization, not a source of truth — a parse
        failure must degrade to "no history", never crash the tool and never
        block a delegation.
        """
        if not self.path.exists():
            return self._blank()
        try:
            raw = yaml.safe_load(self.path.read_text(encoding="utf-8"))
        except yaml.YAMLError:
            return self._blank()
        if not isinstance(raw, dict):
            return self._blank()
        blank = self._blank()
        blank.update(raw)
        if not isinstance(blank.get("methods"), dict):
            blank["methods"] = {}
        if not isinstance(blank.get("revision"), int):
            blank["revision"] = 0
        return blank

    def save(self) -> Path:
        """Atomically write the record.

        `write_text` truncates before writing: a crash mid-write leaves a
        half-parsed YAML file that silently reports every method as unknown.
        A temp file plus `os.replace` makes the swap atomic on POSIX and Windows.
        `allow_unicode=True` keeps Japanese summaries readable instead of
        escaping them to \\uXXXX.
        """
        self.dir.mkdir(parents=True, exist_ok=True)
        payload = yaml.safe_dump(
            self.data,
            default_flow_style=False,
            sort_keys=False,
            allow_unicode=True,
        )
        fd, tmp = tempfile.mkstemp(dir=str(self.dir), prefix=f".{self.component}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return self.path

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def record(
        self,
        method: str,
        status: str,
        *,
        worker: str = "",
        summary: str = "",
        body_hash: str = "",
        tier: str = "",
        gate: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Upsert one method record and bump the global revision.

        The revision is what makes `render_delta` cheap: every write gets a
        monotonically increasing stamp, so a caller holding watermark N can be
        served exactly the records it has not seen.
        """
        if status not in _VALID_STATUSES:
            raise ValueError(f"unknown status {status!r}; expected one of {sorted(_VALID_STATUSES)}")

        self.data["revision"] = int(self.data.get("revision", 0)) + 1
        self.data["updated_at"] = _utc_now()

        entry: dict[str, Any] = {
            "status": status,
            "revision": self.data["revision"],
            "updated_at": self.data["updated_at"],
        }
        if worker:
            entry["worker"] = worker
        if tier:
            entry["tier"] = tier
        if body_hash:
            entry["body_hash"] = body_hash
        if summary:
            entry["summary"] = summary
        if gate:
            entry["gate"] = gate
        if extra:
            entry.update(extra)

        # Keep the prior attempt count so repeated failures are visible without
        # storing a full history (which would grow the file without bound).
        previous = self.data["methods"].get(method)
        attempts = 1
        if isinstance(previous, dict):
            attempts = int(previous.get("attempts", 0)) + 1
        entry["attempts"] = attempts

        self.data["methods"][method] = entry
        return entry

    def set_source_file(self, source_file: str) -> None:
        self.data["source_file"] = source_file

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    @property
    def revision(self) -> int:
        return int(self.data.get("revision", 0))

    def get(self, method: str) -> dict[str, Any] | None:
        entry = self.data["methods"].get(method)
        return entry if isinstance(entry, dict) else None

    def is_current(self, method: str, body_hash: str) -> bool:
        """True when the stored record still describes the code on disk.

        A stored COMPLETED whose `body_hash` no longer matches the file means
        someone edited the method after the handover — the record is stale and
        must not be trusted as "already done".
        """
        entry = self.get(method)
        if not entry or not body_hash:
            return False
        return entry.get("body_hash") == body_hash

    def render_delta(self, since: int = 0, limit: int = 50) -> str:
        """Render only records newer than `since`, newest first.

        This is the read path an agent should use every turn. Returning the
        whole log instead would re-send records the agent already consumed.
        """
        rows = [
            (name, entry)
            for name, entry in self.data["methods"].items()
            if isinstance(entry, dict) and int(entry.get("revision", 0)) > since
        ]
        rows.sort(key=lambda kv: int(kv[1].get("revision", 0)), reverse=True)

        header = f"handover {self.component} rev={self.revision} since={since}"
        if not rows:
            return f"{header}\n  (no changes)"

        lines = [header]
        for name, entry in rows[:limit]:
            bits = [f"  [{entry.get('status','?')}] {name}"]
            if entry.get("tier"):
                bits.append(f"tier={entry['tier']}")
            if entry.get("worker"):
                bits.append(f"by={entry['worker']}")
            if int(entry.get("attempts", 1)) > 1:
                bits.append(f"attempts={entry['attempts']}")
            lines.append(" ".join(bits))
            if entry.get("summary"):
                lines.append(f"      {entry['summary']}")
            gate = entry.get("gate")
            if isinstance(gate, dict) and gate.get("errors"):
                for err in list(gate["errors"])[:3]:
                    lines.append(f"      gate: {err}")
        if len(rows) > limit:
            lines.append(f"  … {len(rows) - limit} older record(s) omitted")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Correction memory (in-context learning, no finetune)
# ---------------------------------------------------------------------------
#
# Retention policy, decided by measurement rather than preference.
#
# Storing the whole file as `previous_bad_code` per failure does not fit: a
# 300-LOC class is ~4,200 tokens, so three failures inject ~12,600 tokens of
# wrong code. The local worker is served at num_ctx=4096 by default and is
# capped at 16,384 here, so by failure two the lesson evicts the actual task.
# Method-scoped bad code is ~250 tokens per failure; a code-free lesson is ~10.
#
# Neither "keep everything" nor "delete on success" is right:
#
#   * Keep everything — unbounded growth, and a context stuffed with several
#     variants of the same wrong code biases a small model toward that shape.
#     Negative examples are weak teachers; a pile of them is worse than a few.
#   * Delete on success — throws away the only durable artifact. The next
#     method with the same mistake repeats it, and if this method is edited
#     later it regresses in exactly the same way.
#
# So corrections are tiered:
#
#   ACTIVE   — while a method is unresolved, keep the last `_ACTIVE_WINDOW`
#              corrections verbatim, each with the failing *method body* only.
#              These are the few-shot examples.
#   PROMOTED — on success, the bad code is dropped and the critique is distilled
#              into a one-line, code-free lesson kept at project scope and
#              deduplicated. Costs ~10 tokens, applies to every future method,
#              and carries no wrong-code pattern to imitate.
#
# Net effect: bounded prompt growth, and lessons that outlive the method that
# taught them.

_ACTIVE_WINDOW = 2
_MAX_BAD_BODY_CHARS = 2400  # ~800 tokens; a body larger than this is not a 7B task
_MAX_CRITIQUE_CHARS = 600
LESSONS_FILE = "_lessons.yaml"

SOURCE_GATE = "gate"
SOURCE_TEST = "test"
SOURCE_CLAUDE = "claude"


def _normalize_lesson(text: str) -> str:
    return " ".join(text.lower().split())


class LessonBook:
    """Project-scoped, code-free lessons distilled from resolved failures."""

    def __init__(self, rtk_dir: str | Path) -> None:
        self.path = Path(rtk_dir) / HANDOVER_DIR / LESSONS_FILE
        self._data: dict[str, Any] | None = None

    @property
    def data(self) -> dict[str, Any]:
        if self._data is None:
            if self.path.exists():
                try:
                    raw = yaml.safe_load(self.path.read_text(encoding="utf-8"))
                except yaml.YAMLError:
                    raw = None
                self._data = raw if isinstance(raw, dict) else {"lessons": []}
            else:
                self._data = {"lessons": []}
            if not isinstance(self._data.get("lessons"), list):
                self._data["lessons"] = []
        return self._data

    def add(self, text: str, *, origin: str = "") -> bool:
        """Record a lesson. Returns False when an equivalent one already exists."""
        text = " ".join(text.split())[:_MAX_CRITIQUE_CHARS]
        if not text:
            return False
        key = _normalize_lesson(text)
        for entry in self.data["lessons"]:
            if isinstance(entry, dict) and _normalize_lesson(str(entry.get("text", ""))) == key:
                entry["hits"] = int(entry.get("hits", 1)) + 1
                return False
        self.data["lessons"].append(
            {"text": text, "origin": origin, "hits": 1, "added_at": _utc_now()}
        )
        return True

    def save(self) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = yaml.safe_dump(
            self.data, default_flow_style=False, sort_keys=False, allow_unicode=True
        )
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix="._lessons.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return self.path

    def top(self, limit: int = 12) -> list[str]:
        """Most-repeated lessons first — the ones the worker keeps needing."""
        rows = [e for e in self.data["lessons"] if isinstance(e, dict) and e.get("text")]
        rows.sort(key=lambda e: int(e.get("hits", 1)), reverse=True)
        return [str(e["text"]) for e in rows[:limit]]


def _corrections_of(entry: dict[str, Any]) -> list[dict[str, Any]]:
    raw = entry.get("corrections")
    return [c for c in raw if isinstance(c, dict)] if isinstance(raw, list) else []


class CorrectionMemory:
    """Correction memory for one component, layered over `HandoverState`."""

    def __init__(self, state: HandoverState) -> None:
        self.state = state
        self.lessons = LessonBook(state.rtk_dir)

    def add(
        self,
        method: str,
        critique: str,
        *,
        source: str = SOURCE_CLAUDE,
        bad_body: str = "",
        body_hash: str = "",
    ) -> dict[str, Any]:
        """Attach a critique to a method, trimming the active window.

        `bad_body` must be the failing *method body*, not the file. The caller
        has exact spans available (`complexity.MethodUnit.body`), so there is no
        reason to store the whole class — and storing the class is what makes
        this mechanism unaffordable.
        """
        methods = self.state.data["methods"]
        entry = methods.get(method)
        if not isinstance(entry, dict):
            entry = {"status": STATUS_PENDING, "attempts": 0}
            methods[method] = entry

        corrections = _corrections_of(entry)
        corrections.append(
            {
                "attempt": len(corrections) + 1,
                "source": source,
                "critique": " ".join(critique.split())[:_MAX_CRITIQUE_CHARS],
                "bad_body": bad_body.strip()[:_MAX_BAD_BODY_CHARS],
                "body_hash": body_hash,
                "added_at": _utc_now(),
            }
        )
        # Bounded window: older attempts are summarized into lessons, not kept
        # verbatim, so the prompt cannot grow without limit.
        for dropped in corrections[:-_ACTIVE_WINDOW]:
            self.lessons.add(str(dropped.get("critique", "")), origin=f"{self.state.component}.{method}")
        entry["corrections"] = corrections[-_ACTIVE_WINDOW:]

        self.state.data["revision"] = int(self.state.data.get("revision", 0)) + 1
        self.state.data["updated_at"] = _utc_now()
        entry["status"] = STATUS_REJECTED
        entry["revision"] = self.state.data["revision"]
        entry["updated_at"] = self.state.data["updated_at"]
        entry["summary"] = f"{source} critique #{len(corrections)}: {corrections[-1]['critique'][:90]}"
        return entry

    def resolve(self, method: str) -> int:
        """Promote a resolved method's corrections to code-free lessons.

        Called after a delegation passes the gate. The bad code is discarded —
        it has no further teaching value once the method is correct, and keeping
        it invites imitation — while every critique survives as a project-scoped
        rule that applies to methods this one never touched.
        """
        entry = self.state.get(method)
        if not entry:
            return 0
        promoted = 0
        for correction in _corrections_of(entry):
            if self.lessons.add(
                str(correction.get("critique", "")),
                origin=f"{self.state.component}.{method}",
            ):
                promoted += 1
        if "corrections" in entry:
            del entry["corrections"]
        return promoted

    def render_for_worker(self, method: str, lesson_limit: int = 8) -> str:
        """Build the few-shot memory block injected into the worker prompt.

        Active corrections for *this* method come first and carry the failing
        body; project lessons follow as a short code-free checklist.
        """
        parts: list[str] = []
        entry = self.state.get(method)
        corrections = _corrections_of(entry) if entry else []

        if corrections:
            parts.append("=== YOUR PREVIOUS ATTEMPTS AT THIS METHOD FAILED. DO NOT REPEAT THESE. ===")
            for correction in corrections:
                parts.append(f"--- Failed attempt {correction.get('attempt', '?')} "
                             f"(rejected by: {correction.get('source', '?')}) ---")
                bad = str(correction.get("bad_body", "")).strip()
                if bad:
                    parts.append("Code you wrote:")
                    parts.append(bad)
                parts.append(f"Why it was rejected: {correction.get('critique', '')}")
            parts.append("")

        rules = self.lessons.top(lesson_limit)
        if rules:
            parts.append("=== PROJECT RULES LEARNED FROM EARLIER REVIEWS ===")
            parts.extend(f"- {rule}" for rule in rules)
            parts.append("")

        return "\n".join(parts)

    def save(self) -> None:
        self.state.save()
        self.lessons.save()
