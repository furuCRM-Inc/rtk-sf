"""
dry_run.py — Local Apex/SOQL syntax validation + session ROI tracker.

Two responsibilities:

1. Dry-run engine
   validate_apex(code)  — regex-based checks for common Apex pitfalls
   validate_soql(query) — SOQL structure and governor limit checks
   No org call, no network, instant feedback.

2. ROI tracker
   record_savings(op, raw_tokens, compressed_tokens)
   get_roi_stats() → formatted summary of tokens + dollars saved this session

   Token cost baseline: Claude Sonnet 4 @ $3 per 1M input tokens.
   Called automatically by mcp_server.py on each suppression tool.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# ROI Tracker
# ---------------------------------------------------------------------------

_TOKEN_COST_PER_MILLION = 3.0  # USD — Claude Sonnet 4 input price

@dataclass
class _RoiSession:
    tokens_saved: int = 0
    dollars_saved: float = 0.0
    operations: list[dict[str, Any]] = field(default_factory=list)
    start_time: float = field(default_factory=time.time)

_session = _RoiSession()


def record_savings(operation: str, raw_tokens: int, compressed_tokens: int) -> None:
    """
    Record a token-saving operation into the session tracker.

    Args:
        operation:        Short label, e.g. 'query_compressed_spec'
        raw_tokens:       Estimated tokens if the raw file had been read
        compressed_tokens: Actual tokens returned by the tool
    """
    saved = max(0, raw_tokens - compressed_tokens)
    dollars = saved * _TOKEN_COST_PER_MILLION / 1_000_000
    _session.tokens_saved += saved
    _session.dollars_saved += dollars
    _session.operations.append({
        "op": operation,
        "raw": raw_tokens,
        "compressed": compressed_tokens,
        "saved": saved,
        "dollars": round(dollars, 6),
    })


def get_roi_stats() -> str:
    """Return a visual terminal summary of tokens and dollars saved this session."""
    elapsed = int(time.time() - _session.start_time)
    minutes, secs = divmod(elapsed, 60)
    duration = f"{minutes}m {secs}s" if minutes else f"{secs}s"

    lines = [
        "── rtk-sf ROI Tracker ──────────────────────────────",
        f"  Tokens saved this session : {_session.tokens_saved:>10,}",
        f"  Dollars saved this session: ${_session.dollars_saved:>9.4f}",
        f"  Suppression operations    : {len(_session.operations):>10}",
        f"  Session duration          : {duration:>10}",
        "─────────────────────────────────────────────────────",
    ]

    if _session.operations:
        lines.append("  Last 5 operations:")
        for op in _session.operations[-5:]:
            lines.append(
                f"    {op['op']:<30}  {op['saved']:>6,} tokens  (${op['dollars']:.4f})"
            )

    if not _session.operations:
        lines.append("  No suppression operations recorded yet this session.")

    lines.append(
        "\n  Token cost basis: Claude Sonnet 4 @ $3.00 / 1M input tokens"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Apex Dry-Run Engine
# ---------------------------------------------------------------------------

_APEX_RULES: list[tuple[re.Pattern[str], str, str]] = [
    (
        re.compile(r"\bfor\b[^{]*\{[^}]*\bSELECT\b", re.IGNORECASE | re.DOTALL),
        "ERROR",
        "SOQL inside a for-loop — governor limit violation (use a List and query before the loop)",
    ),
    (
        re.compile(r"\bfor\b[^{]*\{[^}]*\b(insert|update|delete|upsert|merge|undelete)\b",
                   re.IGNORECASE | re.DOTALL),
        "ERROR",
        "DML inside a for-loop — use a List and bulk DML outside the loop",
    ),
    (
        re.compile(r"\bSystem\.debug\s*\(", re.IGNORECASE),
        "WARN",
        "System.debug() found — remove before production deploy",
    ),
    (
        re.compile(r"//\s*TODO\b", re.IGNORECASE),
        "WARN",
        "TODO comment found — resolve before merging",
    ),
    (
        re.compile(r"\bDatabase\.(insert|update|delete|upsert)\s*\([^,)]+\)\s*;", re.IGNORECASE),
        "INFO",
        "Database DML without allOrNone param — consider explicit error handling",
    ),
    (
        re.compile(r"@future\s*\([^)]*\)\s*public\s+static\s+void\s+\w+\s*\([^)]*(?<!\bString\b)[^)]*\)",
                   re.IGNORECASE),
        "WARN",
        "@future method with non-primitive/non-String parameter — only primitives and String allowed",
    ),
]


def validate_apex(code: str) -> str:
    """
    Run regex-based Apex dry-run checks locally. No org call, instant.

    Checks for:
    - SOQL / DML inside loops (governor limit violations)
    - System.debug in production code
    - TODO comments
    - Unbalanced braces / unclosed strings
    - @future parameter type constraints

    Args:
        code: Apex class or method source code string

    Returns:
        Formatted dry-run report.
    """
    issues: list[tuple[str, str]] = []

    for pattern, level, message in _APEX_RULES:
        if pattern.search(code):
            issues.append((level, message))

    # Structural checks
    opens = code.count("{")
    closes = code.count("}")
    if opens != closes:
        issues.append(("ERROR", f"Unbalanced braces: {opens} '{{' vs {closes} '}}'"))

    # Rough unclosed string check (count unescaped single quotes)
    sq_count = len(re.findall(r"(?<!\\)'", code))
    if sq_count % 2 != 0:
        issues.append(("WARN", "Odd single-quote count — possible unclosed string literal"))

    errors = [m for l, m in issues if l == "ERROR"]
    warns  = [m for l, m in issues if l == "WARN"]
    infos  = [m for l, m in issues if l == "INFO"]

    lines = ["── Apex Dry-Run Report ──────────────────────────────"]
    if not issues:
        lines.append("  ✓  No issues detected")
    else:
        for msg in errors:
            lines.append(f"  ✗  [ERROR] {msg}")
        for msg in warns:
            lines.append(f"  ⚠  [WARN]  {msg}")
        for msg in infos:
            lines.append(f"  ℹ  [INFO]  {msg}")
        lines.append(f"\n  {len(errors)} error(s), {len(warns)} warning(s), {len(infos)} info(s)")

    lines.append("  Note: regex check only — deploy to sandbox to catch compile errors")
    lines.append("─────────────────────────────────────────────────────")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# SOQL Dry-Run Engine
# ---------------------------------------------------------------------------

def validate_soql(query: str) -> str:
    """
    Run basic SOQL structure and governor limit checks locally. No org call.

    Checks for:
    - SELECT / FROM presence
    - SELECT * (not supported in SOQL)
    - LIMIT > 50,000 (governor limit)
    - Unbalanced parentheses
    - Date literal typos (common ones)

    Args:
        query: SOQL query string

    Returns:
        Formatted dry-run report.
    """
    q = query.strip()
    issues: list[tuple[str, str]] = []

    if not re.match(r"\bSELECT\b", q, re.IGNORECASE):
        issues.append(("ERROR", "Query must start with SELECT"))

    if not re.search(r"\bFROM\b", q, re.IGNORECASE):
        issues.append(("ERROR", "Missing FROM clause"))

    if re.search(r"\bSELECT\s+\*", q, re.IGNORECASE):
        issues.append(("ERROR", "SELECT * is not supported in SOQL — list field API names explicitly"))

    limit_match = re.search(r"\bLIMIT\s+(\d+)\b", q, re.IGNORECASE)
    if limit_match:
        limit_val = int(limit_match.group(1))
        if limit_val > 50_000:
            issues.append(("ERROR", f"LIMIT {limit_val:,} exceeds SOQL governor limit (50,000)"))
        elif limit_val > 10_000:
            issues.append(("WARN", f"LIMIT {limit_val:,} is large — consider paginating with OFFSET"))

    opens = q.count("(")
    closes = q.count(")")
    if opens != closes:
        issues.append(("ERROR", f"Unbalanced parentheses: {opens} '(' vs {closes} ')'"))

    # Date literal typos
    bad_dates = re.findall(r"\bTODAY\(\)|LAST_N_DAYS\b(?!\s*:\s*\d)", q, re.IGNORECASE)
    for bd in bad_dates:
        issues.append(("WARN", f"Possible date literal syntax error near '{bd}'"))

    # No WHERE on a large query
    if not re.search(r"\bWHERE\b", q, re.IGNORECASE) and not limit_match:
        issues.append(("WARN", "No WHERE clause and no LIMIT — this will fetch all records"))

    errors = [m for l, m in issues if l == "ERROR"]
    warns  = [m for l, m in issues if l == "WARN"]

    lines = ["── SOQL Dry-Run Report ───────────────────────────────"]
    if not issues:
        lines.append("  ✓  Basic syntax looks valid")
    else:
        for msg in errors:
            lines.append(f"  ✗  [ERROR] {msg}")
        for msg in warns:
            lines.append(f"  ⚠  [WARN]  {msg}")
        lines.append(f"\n  {len(errors)} error(s), {len(warns)} warning(s)")

    lines.append("  Note: regex check only — execute against sandbox to validate fully")
    lines.append("─────────────────────────────────────────────────────")
    return "\n".join(lines)
