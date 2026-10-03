"""
gate.py — machine checks that make a worker's output trustworthy unread.

The whole premise of hybrid delegation is that Claude reads a *verdict* instead
of the generated code. That premise is only sound if something else actually
checked the code. Without a gate, "Qwen completed it" is an unverified claim
and the token saving is bought with correctness.

The gate is deliberately structural rather than semantic — it cannot tell you
the logic is right, so it never claims to. What it does guarantee before a byte
reaches a `.cls` file:

  1. The completion is not truncated.
  2. It parses as exactly one method, with the name that was requested.
  3. Braces, parens, and string literals balance (comment- and string-aware,
     unlike a `code.count("{")` comparison, which miscounts any brace inside a
     literal or comment and would happily approve broken source).
  4. Splicing it changes *that one method and nothing else* — every other
     method's body hash in the file is byte-identical afterwards.
  5. No ERROR-level rule from the existing `dry_run` rule table fires on it.

Check 4 is the one that matters most in practice: it is what catches a small
model that "helpfully" rewrote the whole class and dropped three methods.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rtk_sf.dry_run import _APEX_RULES
from rtk_sf.hybrid.complexity import MethodUnit, extract_methods
from rtk_sf.skeleton import _find_block_end


@dataclass
class GateResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {"ok": self.ok}
        if self.errors:
            out["errors"] = list(self.errors)
        if self.warnings:
            out["warnings"] = list(self.warnings)
        return out


def _balanced(code: str) -> str | None:
    """Return an error message when braces/parens/quotes do not balance.

    Walks the text the same way `skeleton._find_block_end` does, so a `{` inside
    `'label {x}'` or inside `/* ... */` is not counted as structure.
    """
    depth_brace = 0
    depth_paren = 0
    i = 0
    in_str = False
    str_char = ""
    in_line = False
    in_block = False
    while i < len(code):
        ch = code[i]
        nch = code[i + 1] if i + 1 < len(code) else ""
        if in_line:
            if ch == "\n":
                in_line = False
            i += 1
            continue
        if in_block:
            if ch == "*" and nch == "/":
                in_block = False
                i += 2
                continue
            i += 1
            continue
        if in_str:
            if ch == "\\":
                i += 2
                continue
            if ch == str_char:
                in_str = False
            i += 1
            continue
        if ch == "/" and nch == "/":
            in_line = True
            i += 2
            continue
        if ch == "/" and nch == "*":
            in_block = True
            i += 2
            continue
        if ch in ("'", '"'):
            in_str = True
            str_char = ch
            i += 1
            continue
        if ch == "{":
            depth_brace += 1
        elif ch == "}":
            depth_brace -= 1
            if depth_brace < 0:
                return "unbalanced braces: a '}' closes before anything opened"
        elif ch == "(":
            depth_paren += 1
        elif ch == ")":
            depth_paren -= 1
            if depth_paren < 0:
                return "unbalanced parentheses: a ')' closes before anything opened"
        i += 1

    if in_str:
        return "unterminated string literal"
    if in_block:
        return "unterminated block comment"
    if depth_brace != 0:
        return f"unbalanced braces: {depth_brace:+d} unclosed"
    if depth_paren != 0:
        return f"unbalanced parentheses: {depth_paren:+d} unclosed"
    return None


def reindent(candidate: str, indent: str) -> str:
    """Re-indent a worker's method so it sits at the original nesting level.

    A model returns a method at whatever base indentation it likes — usually
    column 0, with a 4-space body. Splicing that into a class leaves the
    signature correct (the original indent is re-applied) but every interior
    line under-indented, which is valid Apex and so passes every syntax check
    while producing a noisy, review-hostile diff. Verified on a real TokyoEdu
    method: the body came back at 4 spaces where the file uses 8.

    The candidate is dedented by its own base indent, then re-indented by the
    target's, which reproduces the file's original shape for both a
    column-0 and an already-indented completion.
    """
    # Leading blank lines are dropped but the first real line keeps its own
    # indentation: that line is what the base indent is measured from. Passing
    # an already-stripped candidate here reports base="" while the interior
    # lines are still indented, which double-indents the body — the bug that
    # an annotated method (`@AuraEnabled` on its own line) exposes first.
    lines = candidate.rstrip().split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    first_body = lines[0] if lines else ""
    base = first_body[: len(first_body) - len(first_body.lstrip())]

    out: list[str] = []
    for line in lines:
        if not line.strip():
            out.append("")
            continue
        # With an empty base, the line's own relative indentation is already
        # correct and must be preserved — lstrip() here would flatten the whole
        # body to one level, which is the bug this replaces.
        stripped = line[len(base) :] if base and line.startswith(base) else line
        out.append(indent + stripped)
    return "\n".join(out)


def check_method_replacement(
    original_source: str,
    target: MethodUnit,
    candidate: str,
    *,
    truncated: bool = False,
) -> tuple[GateResult, str]:
    """Validate a replacement for one method and return the spliced source.

    On failure the returned source is the untouched original — a caller that
    ignores `ok` still cannot corrupt the file.
    """
    errors: list[str] = []
    warnings: list[str] = []

    if truncated:
        errors.append("completion hit the output limit — body is truncated, refusing to splice")
        return GateResult(False, errors, warnings), original_source

    # `candidate` is validated stripped; `raw_candidate` keeps the worker's own
    # indentation, which `reindent` needs to measure the base from.
    raw_candidate = candidate
    candidate = candidate.strip()
    if not candidate:
        errors.append("empty completion")
        return GateResult(False, errors, warnings), original_source

    if not target.splice_safe:
        errors.append(f"'{target.name}' is not a delegatable span: {target.unsafe_reason}")
        return GateResult(False, errors, warnings), original_source

    structural = _balanced(candidate)
    if structural:
        errors.append(structural)
        return GateResult(False, errors, warnings), original_source

    # The completion must be exactly one method, named as requested. A 7B asked
    # for one method frequently returns the whole class.
    parsed = extract_methods(candidate)
    if not parsed:
        errors.append("completion does not parse as an Apex method declaration")
        return GateResult(False, errors, warnings), original_source
    if len(parsed) > 1:
        names = ", ".join(u.name for u in parsed[:5])
        errors.append(f"completion contains {len(parsed)} methods ({names}) — expected exactly 1")
        return GateResult(False, errors, warnings), original_source
    if parsed[0].name.lower() != target.name.lower():
        errors.append(f"completion declares '{parsed[0].name}', expected '{target.name}'")
        return GateResult(False, errors, warnings), original_source

    for pattern, level, message in _APEX_RULES:
        if pattern.search(candidate):
            (errors if level == "ERROR" else warnings).append(message)
    if errors:
        return GateResult(False, errors, warnings), original_source

    # Splice over the full span, annotations included: `_METHOD_SIG` starts its
    # match at the annotation line, so replacing [sig_start, body_close] without
    # returning the annotations would silently drop @AuraEnabled and friends.
    #
    # The span begins at the method's leading indentation (the regex captures
    # `indent` before the modifiers), and `candidate` has been stripped for
    # validation. Re-applying the original indentation is what keeps an
    # identity splice byte-identical — without it every delegated method is
    # silently re-indented to column 0, which is still valid Apex and so would
    # never be caught by a syntax check. Verified across 177 real TokyoEdu
    # methods: identity splice now reproduces each file exactly.
    original_span = original_source[target.sig_start : target.body_close + 1]
    indent = original_span[: len(original_span) - len(original_span.lstrip())]
    body_text = reindent(raw_candidate, indent)
    spliced = (
        original_source[: target.sig_start]
        + body_text
        + original_source[target.body_close + 1 :]
    )

    structural = _balanced(spliced)
    if structural:
        errors.append(f"spliced file does not balance: {structural}")
        return GateResult(False, errors, warnings), original_source

    # The invariant that lets Claude skip reading the diff: nothing but the
    # target method moved.
    before = {u.name: u.body_hash for u in extract_methods(original_source)}
    after = {u.name: u.body_hash for u in extract_methods(spliced)}

    lost = sorted(set(before) - set(after))
    gained = sorted(set(after) - set(before))
    if lost:
        errors.append(f"splice would remove method(s): {', '.join(lost)}")
    if gained:
        errors.append(f"splice would add unexpected method(s): {', '.join(gained)}")

    collateral = sorted(
        name
        for name in set(before) & set(after)
        if name.lower() != target.name.lower() and before[name] != after[name]
    )
    if collateral:
        errors.append(f"splice would also modify: {', '.join(collateral)}")

    if errors:
        return GateResult(False, errors, warnings), original_source

    if after.get(target.name) == before.get(target.name):
        warnings.append("method body is unchanged — worker returned the input verbatim")

    return GateResult(True, errors, warnings), spliced


def check_whole_file(source: str) -> GateResult:
    """Structural sanity check for a file, used before and after any write."""
    problem = _balanced(source)
    if problem:
        return GateResult(False, [problem])
    if not extract_methods(source):
        return GateResult(True, [], ["no top-level methods detected"])
    return GateResult(True)


__all__ = ["GateResult", "check_method_replacement", "check_whole_file", "reindent", "_find_block_end"]
