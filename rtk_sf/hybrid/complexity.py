"""
complexity.py — deterministic per-method complexity and risk scoring for Apex.

Answers one question for the orchestrator: *may a small local model touch this
method, or must Claude design it?* The answer is derived from the source, never
from a human-authored `complexity: LOW` label — a label drifts the moment the
code changes, and a wrong label routes a high-blast-radius method to a 7B.

Two independent outputs per method:

  score  — a cyclomatic-style integer over the method body.
  blockers — risk signals that force HIGH regardless of score. A three-line
             method that does DML inside a loop, opens a Savepoint, or is an
             @AuraEnabled entry point is not "simple" at any length.

Spans come from `rtk_sf.skeleton`'s comment- and string-aware brace scanner, so
a brace inside `'a { b'` or a block comment cannot shift a body boundary — the
same spans are later used to splice a generated body back in.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

from rtk_sf.skeleton import _METHOD_SIG, _NOT_A_METHOD, _find_block_end

# ---------------------------------------------------------------------------
# Scoring tables
# ---------------------------------------------------------------------------

# Decision points. Each occurrence adds 1 to the score.
_BRANCH = re.compile(
    r"\b(if|else\s+if|for|while|do|catch|when)\b|&&|\|\||\?\s*[^:]+\s*:", re.IGNORECASE
)
_LOOP = re.compile(r"\b(for|while|do)\b", re.IGNORECASE)
_SOQL = re.compile(r"\[\s*SELECT\b|\bDatabase\.query\s*\(", re.IGNORECASE)
_DML = re.compile(
    r"\b(insert|update|upsert|delete|undelete|merge)\s+[\w\[(]|"
    r"\bDatabase\.(insert|update|upsert|delete|undelete|merge)\s*\(",
    re.IGNORECASE,
)
_TRY = re.compile(r"\btry\s*\{", re.IGNORECASE)

# Tokens that can never be a return type. `_METHOD_SIG` has no way to express
# "this declaration has no return type", so a constructor only matches by
# letting an access modifier be captured as `ret`:
#
#     public RowResult(Integer idx, Boolean ok, String msg) {
#     ^mods=""  ^ret="public"      ^name="RowResult"
#
# Verified on TokyoEdu: `CsvImportController.RowResult` is detected this way and
# was being routed to the local worker as a LOW-complexity method. The worker is
# told to keep the name and signature, so its most likely repair of an
# "invalid" declaration is to add a return type — silently turning a
# constructor into an ordinary method, which still parses and still splices.
# Constructors are therefore identified and excluded from delegation.
_MODIFIER_WORDS = frozenset(
    {
        "public", "private", "protected", "global", "static", "override",
        "virtual", "abstract", "final", "transient", "testmethod", "webservice",
        "with", "without", "inherited", "sharing",
    }
)

# Hard blockers. Presence forces tier HIGH — a wrong edit here costs data,
# security, or governor headroom, and a small model has no way to know what it
# does not know. Measured against TokyoEdu (179 real methods) when tuning:
# these fire on 42 methods, which is the intended shape.
_BLOCKERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "soql-in-loop",
        re.compile(r"\b(for|while)\b[^{;]*\{(?:[^{}]|\{[^{}]*\})*?\[\s*SELECT\b", re.IGNORECASE),
    ),
    (
        "dml-in-loop",
        re.compile(
            r"\b(for|while)\b[^{;]*\{(?:[^{}]|\{[^{}]*\})*?"
            r"\b(insert|update|upsert|delete|undelete|merge)\s+[\w\[(]",
            re.IGNORECASE,
        ),
    ),
    ("transaction-control", re.compile(r"\b(Savepoint|setSavepoint|rollback)\b", re.IGNORECASE)),
    ("partial-dml", re.compile(r"\bDatabase\.(insert|update|upsert|delete)\s*\(", re.IGNORECASE)),
    ("callout", re.compile(r"\b(HttpRequest|HttpResponse|WebServiceCallout|Http)\s*\(?", re.IGNORECASE)),
    ("sharing-bypass", re.compile(r"\bwithout\s+sharing\b|\bSystem\.runAs\b", re.IGNORECASE)),
    ("crud-fls", re.compile(r"\b(isAccessible|isCreateable|isUpdateable|isDeletable|stripInaccessible)\b", re.IGNORECASE)),
    ("email-send", re.compile(r"\bMessaging\.send\w*\s*\(", re.IGNORECASE)),
    ("batch-apex", re.compile(r"\bDatabase\.(executeBatch|Batchable|Stateful)\b", re.IGNORECASE)),
)

# Risk flags. These do NOT force HIGH — they add weight and raise the required
# verification depth. Measured on TokyoEdu: treating @AuraEnabled as a hard
# blocker forced 60 of 179 methods (34%) to Claude on a normal LWC-based
# project, which makes delegation pointless. An @AuraEnabled one-line getter is
# an entry point worth testing, not work a 7B cannot do — so it raises the gate
# (deploy/test required) instead of vetoing the route.
_FLAGS: tuple[tuple[str, re.Pattern[str], int], ...] = (
    ("remote-entry", re.compile(r"@(auraenabled|restresource|httpget|httppost|httpdelete)\b", re.IGNORECASE), 3),
    ("async-entry", re.compile(r"@(future|queueable|schedulable|invocablemethod)\b", re.IGNORECASE), 4),
    ("test-method", re.compile(r"@(istest|testsetup)\b", re.IGNORECASE), 0),
)

# Tier thresholds. Deliberately conservative: the cost of sending an easy method
# to Claude is a few hundred tokens; the cost of sending a hard one to a 7B is a
# broken class.
_LOW_MAX_SCORE = 6
_LOW_MAX_LOC = 40
_MEDIUM_MAX_SCORE = 14
_MEDIUM_MAX_LOC = 90


@dataclass
class MethodUnit:
    """One top-level method, with its exact source span and risk profile."""

    name: str
    signature: str
    sig_start: int
    body_open: int
    body_close: int
    body: str
    loc: int
    metrics: dict[str, int]
    score: int
    tier: str
    blockers: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    splice_safe: bool = True
    is_constructor: bool = False
    unsafe_reason: str = ""

    @property
    def body_hash(self) -> str:
        """Hash of this method's body only.

        Per-method, not per-file: a file hash marks every method dirty whenever
        any one of them changes, which makes method-level handover meaningless.
        """
        return hashlib.sha256(self.body.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "signature": self.signature.strip(),
            "loc": self.loc,
            "score": self.score,
            "tier": self.tier,
            "blockers": list(self.blockers),
            "flags": list(self.flags),
            "splice_safe": self.splice_safe,
            "is_constructor": self.is_constructor,
            "body_hash": self.body_hash,
            "metrics": dict(self.metrics),
        }


def _max_nesting(body: str) -> int:
    """Deepest brace nesting inside a method body, ignoring strings/comments."""
    depth = 0
    best = 0
    i = 0
    in_str = False
    str_char = ""
    in_line = False
    in_block = False
    while i < len(body):
        ch = body[i]
        nch = body[i + 1] if i + 1 < len(body) else ""
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
            depth += 1
            best = max(best, depth)
        elif ch == "}":
            depth -= 1
        i += 1
    return best


def _strip_noise(body: str) -> str:
    """Remove comments and string literals before pattern counting.

    Without this, `// TODO: insert the record` counts as DML and a message like
    `'... for each ...'` counts as a loop.
    """
    out: list[str] = []
    i = 0
    in_str = False
    str_char = ""
    in_line = False
    in_block = False
    while i < len(body):
        ch = body[i]
        nch = body[i + 1] if i + 1 < len(body) else ""
        if in_line:
            if ch == "\n":
                in_line = False
                out.append("\n")
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
            out.append("''")
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _classify(score: int, loc: int, blockers: list[str], splice_safe: bool = True) -> str:
    if blockers or not splice_safe:
        return "HIGH"
    if score <= _LOW_MAX_SCORE and loc <= _LOW_MAX_LOC:
        return "LOW"
    if score <= _MEDIUM_MAX_SCORE and loc <= _MEDIUM_MAX_LOC:
        return "MEDIUM"
    return "HIGH"


def _scan(source: str) -> list[dict[str, Any]]:
    """Raw, non-recursive pass: every non-overlapping top-level method block.

    Kept separate from `extract_methods` so a unit can be re-scanned in
    isolation without recursing.
    """
    blocks: list[dict[str, Any]] = []
    covered_until = -1
    for m in _METHOD_SIG.finditer(source):
        name = m.group("name")
        ret = (m.group("ret") or "").strip().lower()
        if name.lower() in _NOT_A_METHOD or ret in _NOT_A_METHOD:
            continue
        if m.start() < covered_until:
            continue
        body_open = m.end() - 1
        body_close = _find_block_end(source, body_open)
        covered_until = body_close + 1
        blocks.append(
            {
                "name": name,
                "ret": ret,
                "sig_start": m.start(),
                "body_open": body_open,
                "body_close": body_close,
            }
        )
    return blocks


def extract_methods(source: str) -> list[MethodUnit]:
    """Return every top-level method in an Apex class with span and risk profile.

    Nested matches (inner-class methods, control flow that looks like a
    signature) are skipped the same way `build_skeleton` skips them, so spans
    stay non-overlapping and safe to splice.

    Each unit is re-scanned in isolation to set `splice_safe`. `_METHOD_SIG`
    cannot express "has no return type", so an inner-class constructor
    (`public SaveTableResult(String x) {`) matches only by borrowing preceding
    text as its return type — the resulting span is not a real method and must
    never be replaced. Verified against TokyoEdu: 2 of 179 detections are this
    shape, and both are now marked unsafe rather than offered for delegation.
    """
    units: list[MethodUnit] = []
    prev_body_close = -1

    for block in _scan(source):
        name = block["name"]
        sig_start = block["sig_start"]
        body_open = block["body_open"]
        body_close = block["body_close"]

        body = source[body_open : body_close + 1]
        clean = _strip_noise(body)

        # Annotations and modifiers sit before the signature, not in the body —
        # @AuraEnabled above the method must still be seen as a risk signal.
        # The window must stop at the previous method's closing brace: reaching
        # past it attributes the previous body's risk signals to this method.
        head_start = max(prev_body_close + 1, sig_start - 200)
        head = source[head_start:body_open]

        metrics = {
            "branches": len(_BRANCH.findall(clean)),
            "loops": len(_LOOP.findall(clean)),
            "soql": len(_SOQL.findall(clean)),
            "dml": len(_DML.findall(clean)),
            "try_blocks": len(_TRY.findall(clean)),
            "nesting": _max_nesting(body),
        }
        loc = len([ln for ln in body.splitlines() if ln.strip()])

        blockers = [label for label, pattern in _BLOCKERS if pattern.search(clean + head)]
        flags: list[str] = []
        flag_weight = 0
        for label, pattern, weight in _FLAGS:
            if pattern.search(clean + head):
                flags.append(label)
                flag_weight += weight

        score = (
            metrics["branches"]
            + metrics["loops"] * 2
            + metrics["soql"] * 2
            + metrics["dml"] * 3
            + metrics["try_blocks"]
            + max(0, metrics["nesting"] - 2) * 2
            + loc // 25
            + flag_weight
        )

        # A span is splice-safe only when its own text re-scans to exactly the
        # same single method. This is the invariant the splice depends on.
        #
        # The rescan uses the *stripped* span because that is the shape a worker
        # actually returns (no leading indentation). Scanning the unstripped
        # span is more permissive and disagrees with the gate: an inner-class
        # constructor only matches `_METHOD_SIG` while preceding whitespace is
        # present, so it would be marked safe here and then rejected at the
        # gate — a route the planner should never have offered.
        own = source[sig_start : body_close + 1].strip()
        rescanned = _scan(own)
        splice_safe = len(rescanned) == 1 and rescanned[0]["name"].lower() == name.lower()
        unsafe_reason = "" if splice_safe else "span does not re-parse to a single matching method"

        # A declaration whose return type is an access modifier has no return
        # type at all — it is a constructor, and must not be delegated.
        is_constructor = block.get("ret", "").strip().lower() in _MODIFIER_WORDS
        if is_constructor:
            splice_safe = False
            unsafe_reason = "constructor (no return type) — rewriting risks turning it into a method"

        units.append(
            MethodUnit(
                name=name,
                signature=source[sig_start:body_open].strip(),
                sig_start=sig_start,
                body_open=body_open,
                body_close=body_close,
                body=body,
                loc=loc,
                metrics=metrics,
                score=score,
                tier=_classify(score, loc, blockers, splice_safe),
                blockers=blockers,
                flags=flags,
                splice_safe=splice_safe,
                is_constructor=is_constructor,
                unsafe_reason=unsafe_reason,
            )
        )
        prev_body_close = body_close

    return units


def find_method(source: str, method_name: str) -> MethodUnit | None:
    """Return one named method unit, or None when the class has no such method."""
    target = method_name.lower()
    for unit in extract_methods(source):
        if unit.name.lower() == target:
            return unit
    return None
