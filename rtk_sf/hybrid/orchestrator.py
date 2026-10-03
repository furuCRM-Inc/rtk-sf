"""
orchestrator.py — routes Apex method work between a local worker and Claude.

Token model, stated plainly because the usual framing is wrong.

Claude's prompt cache is a prefix match over the rendered request
(`tools` -> `system` -> `messages`). A file's contents reach the model as a
tool result appended at the *end* of `messages`, so the order of keys *inside*
a YAML file has no effect on cache hits: re-reading a file appends a fresh full
copy and is billed as new input either way, whether the volatile section sits
at the top or the bottom. Arranging a spec as "static prefix / dynamic tail"
buys nothing on its own.

What actually reduces cost here is returning fewer tokens per call, which this
module does in four ways:

  1. Delegation is method-scoped. The worker is sent one method, not the file,
     so it cannot exceed its context window and cannot rewrite the class.
  2. Claude reads a verdict, not code — and the verdict is trustworthy because
     `gate.py` proved the splice touched exactly one method.
  3. State reads are deltas (`HandoverState.render_delta`), tens of tokens.
  4. A batch of tasks returns one consolidated result. This matters for cache
     behaviour: a turn that appends more than ~20 positions can push the
     previous cache entry out of the lookback window and force the whole
     conversation to be rewritten, so fanning out one tool call per method is
     actively harmful.

Three tools are registered rather than one per operation: tool definitions
render at position 0 of the prompt, so adding or reordering them invalidates
every cache tier. The tool surface is kept small and stable on purpose.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rtk_sf.hybrid import state as state_mod
from rtk_sf.hybrid.complexity import MethodUnit, extract_methods, find_method
from rtk_sf.hybrid.gate import check_method_replacement, check_whole_file
from rtk_sf.hybrid.state import CorrectionMemory, HandoverState

RTK_DIR = ".rtk-sf"

# Which tiers a local worker may take. MEDIUM is opt-in: it is the band where a
# 7B is plausible but unverified, so the caller has to ask for it.
_LOCAL_TIERS = frozenset({"LOW"})
_LOCAL_TIERS_EXTENDED = frozenset({"LOW", "MEDIUM"})

_SYSTEM_PROMPT = (
    "You are a precise Apex refactoring tool. You rewrite exactly one method.\n"
    "Rules:\n"
    "1. Output ONLY the complete method, from its first annotation or modifier "
    "through its closing brace.\n"
    "2. Do NOT output the class declaration, other methods, imports, or any "
    "explanation.\n"
    "3. Keep the method name and parameter list exactly as given.\n"
    "4. Preserve all existing annotations.\n"
    "5. Apex has no 'var'; use explicit types. String comparison uses "
    "String.isBlank / equals, not ==.\n"
)


@dataclass
class TaskResult:
    method: str
    status: str
    tier: str = ""
    detail: str = ""
    errors: list[str] | None = None
    seconds: float = 0.0
    output_tokens: int = 0


class Orchestrator:
    """Plan, delegate, and record method-level work for one Apex component."""

    def __init__(
        self,
        project_root: str | Path = ".",
        *,
        base_url: str | None = None,
        model: str | None = None,
        allow_medium: bool = False,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.rtk_dir = self.project_root / RTK_DIR
        self.base_url = base_url
        self.model = model
        self.allow_medium = allow_medium
        self._worker: Any = None
        # Files already backed up during the current delegate() batch.
        self._backed_up: set[Path] = set()

    # ------------------------------------------------------------------
    # Worker
    # ------------------------------------------------------------------

    def worker(self) -> Any:
        """Detect the local worker once per orchestrator instance.

        Imported lazily: CONTRIBUTING.md requires rtk-sf to work fully offline,
        so nothing that can reach a model is imported at module load.
        """
        if self._worker is None:
            from rtk_sf.hybrid.worker import DEFAULT_BASE_URL, detect_worker

            self._worker = detect_worker(self.base_url or DEFAULT_BASE_URL, model=self.model)
        return self._worker

    # ------------------------------------------------------------------
    # Source resolution
    # ------------------------------------------------------------------

    def _resolve_source(self, component: str) -> Path | None:
        """Locate a component .cls file via its generated spec, then by name."""
        import yaml

        from rtk_sf.skeleton import _resolve_source_path

        spec_file = self.rtk_dir / "specs" / f"{component}.yaml"
        if spec_file.exists():
            try:
                spec = yaml.safe_load(spec_file.read_text(encoding="utf-8"))
            except yaml.YAMLError:
                spec = None
            if isinstance(spec, dict) and spec.get("file"):
                resolved = _resolve_source_path(str(spec["file"]), self.project_root)
                if resolved is not None:
                    return resolved

        for hit in self.project_root.rglob(f"{component}.cls"):
            if hit.is_file():
                return hit
        return None

    def _assert_inside_project(self, path: Path) -> Path:
        """Refuse to write outside the project root.

        The source path originates in a generated YAML file; treating that as a
        trusted write target lets a bad or hand-edited spec redirect a write
        anywhere on the filesystem.
        """
        resolved = path.resolve()
        try:
            resolved.relative_to(self.project_root)
        except ValueError as exc:
            raise ValueError(f"refusing to write outside the project: {resolved}") from exc
        return resolved

    def _write_source(self, path: Path, content: str) -> None:
        """Back up once per batch, then atomically replace the source file.

        A worker output replaces real source, so the pre-batch content is kept
        as `<name>.cls.rtk-bak` and the swap is atomic — a crash mid-write must
        not leave a half-written Apex class on disk.

        The backup is written only on the first edit of a file in a batch.
        Copying on every write makes the second task overwrite the first task's
        backup, so `.rtk-bak` would hold "the file before the last edit"
        instead of "the file before the batch" — and a two-task batch could
        then only be rolled back halfway.
        """
        path = self._assert_inside_project(path)
        if path not in self._backed_up:
            shutil.copy2(path, path.with_suffix(path.suffix + ".rtk-bak"))
            self._backed_up.add(path)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(content)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ------------------------------------------------------------------
    # Plan
    # ------------------------------------------------------------------

    def plan(self, component: str, since: int = 0) -> str:
        """Return a compact routing table for every method in a component."""
        source_path = self._resolve_source(component)
        if source_path is None:
            return f"Error: no .cls file found for component '{component}'."

        source = source_path.read_text(encoding="utf-8", errors="replace")
        units = extract_methods(source)
        if not units:
            return f"{component}: no top-level methods detected in {source_path.name}."

        worker = self.worker()
        local_tiers = _LOCAL_TIERS_EXTENDED if self.allow_medium else _LOCAL_TIERS
        st = HandoverState(self.rtk_dir, component)

        lines = [
            f"plan {component} ({source_path.name}, {len(units)} methods)",
            f"worker: {worker.label}" + (f" — {worker.detail}" if not worker.available else ""),
            f"local tiers: {'LOW+MEDIUM' if self.allow_medium else 'LOW only'}",
            "",
        ]

        local: list[str] = []
        claude: list[str] = []
        done: list[str] = []

        for unit in units:
            if st.is_current(unit.name, unit.body_hash):
                entry = st.get(unit.name) or {}
                if entry.get("status") == state_mod.STATUS_COMPLETED:
                    done.append(unit.name)
                    continue

            route_local = worker.available and unit.tier in local_tiers and unit.splice_safe
            note = []
            if unit.blockers:
                note.append("blockers=" + ",".join(unit.blockers))
            if unit.flags:
                note.append("flags=" + ",".join(unit.flags))
            if not unit.splice_safe:
                note.append("NOT-DELEGATABLE:" + (unit.unsafe_reason.split(" —")[0] or "unsafe span"))
            row = (
                f"  {unit.name} [{unit.tier}] score={unit.score} loc={unit.loc}"
                + (" " + " ".join(note) if note else "")
            )
            (local if route_local else claude).append(row)

        if local:
            lines.append(f"-> LOCAL WORKER ({len(local)}):")
            lines.extend(local)
        if claude:
            lines.append(f"-> CLAUDE ({len(claude)}):")
            lines.extend(claude)
        if done:
            lines.append(f"-> ALREADY COMPLETED, hash current ({len(done)}): {', '.join(done)}")

        delta = st.render_delta(since=since)
        if "no changes" not in delta:
            lines.extend(["", delta])
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Delegate
    # ------------------------------------------------------------------

    def _build_prompt(self, component: str, unit: MethodUnit, instruction: str, memory: str) -> str:
        from rtk_sf.skeleton import build_skeleton

        source_path = self._resolve_source(component)
        context = ""
        if source_path is not None:
            source = source_path.read_text(encoding="utf-8", errors="replace")
            # Skeleton, not the file: class-level state and sibling signatures
            # are the context that matters, and collapsing the other bodies is
            # what keeps the prompt inside the worker window.
            context = build_skeleton(source, focus_methods=[])

        parts = []
        if memory:
            parts.append(memory)
        if context:
            parts.append("=== CLASS CONTEXT (other method bodies collapsed) ===")
            parts.append(context)
        parts.append("=== METHOD TO REWRITE ===")
        parts.append(unit.body if False else f"{unit.signature}\n{unit.body}")
        parts.append("=== TASK ===")
        parts.append(instruction)
        parts.append(f"\nReturn only the complete rewritten '{unit.name}' method, nothing else.")
        return "\n".join(parts)

    def delegate(self, component: str, tasks: list[dict[str, str]]) -> str:
        """Run a batch of method-scoped delegations and return one verdict.

        Batched deliberately: one tool result per call keeps the number of
        appended positions small, which is what protects the conversation cache
        entry from falling out of the lookback window.
        """
        source_path = self._resolve_source(component)
        if source_path is None:
            return f"Error: no .cls file found for component '{component}'."
        if not tasks:
            return "Error: no tasks supplied."

        worker = self.worker()
        if not worker.available:
            return (
                f"No local worker available ({worker.detail}).\n"
                "Handle these methods directly; nothing was written."
            )

        self._backed_up.clear()

        from rtk_sf.hybrid.worker import generate

        st = HandoverState(self.rtk_dir, component)
        try:
            st.set_source_file(str(source_path.relative_to(self.project_root)))
        except ValueError:
            st.set_source_file(str(source_path))
        mem = CorrectionMemory(st)
        local_tiers = _LOCAL_TIERS_EXTENDED if self.allow_medium else _LOCAL_TIERS
        results: list[TaskResult] = []

        for task in tasks:
            method = str(task.get("method", "")).strip()
            instruction = str(task.get("instruction", "")).strip()
            if not method or not instruction:
                results.append(
                    TaskResult(
                        method or "(unnamed)",
                        "SKIPPED",
                        detail="method and instruction are both required",
                    )
                )
                continue

            # Re-read per task: an earlier task in this batch may have written.
            source = source_path.read_text(encoding="utf-8", errors="replace")
            unit = find_method(source, method)
            if unit is None:
                results.append(TaskResult(method, "SKIPPED", detail="method not found in class"))
                st.record(method, state_mod.STATUS_FAILED, summary="method not found")
                continue

            if not unit.splice_safe:
                results.append(
                    TaskResult(method, "REFUSED", tier=unit.tier, detail=unit.unsafe_reason)
                )
                st.record(
                    method,
                    state_mod.STATUS_NEEDS_CLAUDE,
                    tier=unit.tier,
                    summary=unit.unsafe_reason,
                )
                continue

            if unit.tier not in local_tiers:
                detail = f"tier {unit.tier}"
                if unit.blockers:
                    detail += " (" + ", ".join(unit.blockers) + ")"
                results.append(TaskResult(method, "REFUSED", tier=unit.tier, detail=detail))
                st.record(
                    method,
                    state_mod.STATUS_NEEDS_CLAUDE,
                    tier=unit.tier,
                    body_hash=unit.body_hash,
                    summary=f"routed to Claude: {detail}",
                )
                continue

            memory = mem.render_for_worker(method)
            prompt = self._build_prompt(component, unit, instruction, memory)
            st.record(
                method,
                state_mod.STATUS_DELEGATED,
                worker=worker.label,
                tier=unit.tier,
                body_hash=unit.body_hash,
                summary="sent to local worker",
            )

            gen = generate(worker, _SYSTEM_PROMPT, prompt)
            if not gen.ok:
                results.append(TaskResult(method, "FAILED", tier=unit.tier, detail=gen.error))
                st.record(
                    method,
                    state_mod.STATUS_FAILED,
                    worker=worker.label,
                    tier=unit.tier,
                    body_hash=unit.body_hash,
                    summary=gen.error,
                )
                continue

            verdict, spliced = check_method_replacement(
                source, unit, gen.text, truncated=gen.truncated
            )
            if not verdict.ok:
                # Gate failures become correction memory at zero Claude cost —
                # the next attempt learns from a machine verdict instead of
                # waiting for an architect review.
                mem.add(
                    method,
                    "; ".join(verdict.errors),
                    source=state_mod.SOURCE_GATE,
                    bad_body=gen.text,
                    body_hash=unit.body_hash,
                )
                results.append(
                    TaskResult(
                        method,
                        "REJECTED",
                        tier=unit.tier,
                        errors=list(verdict.errors),
                        seconds=gen.seconds,
                        output_tokens=gen.output_tokens,
                    )
                )
                continue

            whole = check_whole_file(spliced)
            if not whole.ok:
                mem.add(
                    method,
                    "; ".join(whole.errors),
                    source=state_mod.SOURCE_GATE,
                    bad_body=gen.text,
                    body_hash=unit.body_hash,
                )
                results.append(
                    TaskResult(method, "REJECTED", tier=unit.tier, errors=list(whole.errors))
                )
                continue

            self._write_source(source_path, spliced)
            new_unit = find_method(spliced, method)
            promoted = mem.resolve(method)
            st.record(
                method,
                state_mod.STATUS_COMPLETED,
                worker=worker.label,
                tier=unit.tier,
                body_hash=new_unit.body_hash if new_unit else "",
                summary=instruction[:140],
                gate=verdict.to_dict(),
                extra={"lessons_promoted": promoted} if promoted else None,
            )
            results.append(
                TaskResult(
                    method,
                    "COMPLETED",
                    tier=unit.tier,
                    detail=", ".join(verdict.warnings) if verdict.warnings else "",
                    seconds=gen.seconds,
                    output_tokens=gen.output_tokens,
                )
            )

        mem.save()
        return self._render_batch(component, source_path, worker, results, st)

    @staticmethod
    def _render_batch(
        component: str,
        source_path: Path,
        worker: Any,
        results: list[TaskResult],
        st: HandoverState,
    ) -> str:
        counts: dict[str, int] = {}
        for r in results:
            counts[r.status] = counts.get(r.status, 0) + 1
        summary = " ".join(f"{k}={v}" for k, v in sorted(counts.items()))

        lines = [
            f"delegate {component} ({source_path.name}) via {worker.label}",
            f"result: {summary}  rev={st.revision}",
            "",
        ]
        for r in results:
            head = f"  [{r.status}] {r.method}"
            if r.tier:
                head += f" tier={r.tier}"
            if r.output_tokens:
                head += f" {r.output_tokens}tok/{r.seconds:.0f}s"
            lines.append(head)
            if r.detail:
                lines.append(f"      {r.detail}")
            for err in (r.errors or [])[:3]:
                lines.append(f"      gate: {err}")

        rejected = [r.method for r in results if r.status == "REJECTED"]
        needs_claude = [r.method for r in results if r.status == "REFUSED"]
        if rejected:
            lines.append("")
            lines.append(
                "Rejected output was NOT written; the gate verdict is stored as "
                "correction memory, so retrying these feeds the worker its own "
                f"failure: {', '.join(rejected)}"
            )
        if needs_claude:
            lines.append(f"Design these yourself: {', '.join(needs_claude)}")
        if any(r.status == "COMPLETED" for r in results):
            lines.append("")
            lines.append(
                "Completed methods passed the splice gate: exactly one method "
                "changed per edit, every other body byte-identical, braces and "
                "literals balanced. Reading the generated code is not required "
                "to trust that; run the class tests to judge behaviour."
            )
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Critique
    # ------------------------------------------------------------------

    def critique(self, component: str, method: str, text: str) -> str:
        """Record a review finding so the next local attempt learns from it."""
        source_path = self._resolve_source(component)
        if source_path is None:
            return f"Error: no .cls file found for component '{component}'."

        source = source_path.read_text(encoding="utf-8", errors="replace")
        unit = find_method(source, method)
        st = HandoverState(self.rtk_dir, component)
        mem = CorrectionMemory(st)
        mem.add(
            method,
            text,
            source=state_mod.SOURCE_CLAUDE,
            # The failing method body only. Storing the whole file here is what
            # makes correction memory unaffordable: three failures on a 300-LOC
            # class is ~12,600 tokens of wrong code, past the worker window.
            bad_body=unit.body if unit else "",
            body_hash=unit.body_hash if unit else "",
        )
        mem.save()
        return (
            f"critique recorded for {component}.{method} (rev={st.revision}).\n"
            "The next local attempt at this method receives it as a few-shot "
            "example; once the method passes, it is distilled into a code-free "
            "project rule that applies to every other method."
        )

    def state(self, component: str, since: int = 0) -> str:
        """Return the handover delta for a component."""
        return HandoverState(self.rtk_dir, component).render_delta(since=since)
