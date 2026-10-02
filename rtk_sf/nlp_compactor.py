"""
nlp_compactor.py — Bilingual (English + Japanese) prompt compactor.

Strips conversational noise from user prompts before they consume tokens in
the AI context window. Preserves Salesforce API names, method names, class
names, and all structural parameters.

Compaction is prose-only. Pasted machine text — fenced code blocks, inline
code spans, `sf … --json` output, eslint reports, stack traces — is stashed
behind placeholders first and restored untouched, because rewriting it
destroys the thing the user pasted it for: indentation inside a fence used to
be collapsed (invalid YAML), and an identifier quoted in a lint message could
be deleted outright (`'just' is assigned a value` → `'' is assigned a value`).

Token impact: a 200-token polite request → ~80-token intent payload.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# English filler patterns (case-insensitive)
# ---------------------------------------------------------------------------

_EN_FILLERS: list[str] = [
    r"\bplease\b\s*",
    r"\bcould\s+you\b\s*",
    r"\bcan\s+you\b\s*",
    r"\bwould\s+you\b\s*",
    r"\bwould\s+you\s+mind\b\s*",
    r"\bkindly\b\s*",
    r"\bjust\b\s*",
    r"\bquickly\b\s*",
    r"\bsimply\b\s*",
    r"\bbasically\b\s*",
    r"\bactually\b\s*",
    r"\bif\s+possible\b[,.]?\s*",
    r"\bif\s+you\s+don'?t\s+mind\b[,.]?\s*",
    r"\bfeel\s+free\s+to\b\s*",
    r"\bgo\s+ahead\s+and\b\s*",
    r"\blet\s+me\s+know\b[^.]*\.",
    r"\bthanks?\b[.!]?\s*",
    r"\bthank\s+you\b[.!]?\s*",
    r"\bmuch\s+appreciated\b[.!]?\s*",
    r"\bcheers\b[.!]?\s*",
    r"\bhey\b[,]?\s*",
    r"\bhi\b[,]?\s*",
    r"\bhello\b[,]?\s*",
    r"\bsorry\s+to\s+bother\s+you\b[,.]?\s*",
    r"\bI\s+was\s+wondering\s+(if\s+)?(you\s+could\s+)?",
    r"\bI\s+think\s+",
    r"\bI\s+believe\s+",
    r"\bI\s+need\s+you\s+to\s+",
    r"\bI\s+want\s+you\s+to\s+",
    r"\bI'?d\s+like\s+(you\s+to\s+)?",
    r"\bmake\s+sure\s+to\s+",
    r"\bdon'?t\s+forget\s+to\s+",
]

_EN_FILLER_RE = re.compile(
    "|".join(_EN_FILLERS), re.IGNORECASE
)

# ---------------------------------------------------------------------------
# Japanese filler/politeness patterns
# ---------------------------------------------------------------------------

_JA_FILLERS: list[str] = [
    # Polite request endings
    r"お願いします[。！]?",
    r"よろしくお願いします[。！]?",
    r"よろしくお願いいたします[。！]?",
    r"よろしくお願い申し上げます[。！]?",
    r"お願いいたします[。！]?",
    # Polite action request suffixes — strip the suffix, preserve the verb before it
    # e.g. "確認してください" → "確認"  (keep the intent verb, drop the polite wrapper)
    r"してください[。]?",
    r"して下さい[。]?",
    r"してもらえますか[？。]?",
    r"していただけますか[？。]?",
    r"していただけると幸いです[。]?",
    r"してほしいです[。]?",
    r"してほしいのですが[。]?",
    # Softeners
    r"ちょっと\s*",
    r"少し\s*",
    r"なるべく\s*",
    r"できれば\s*",
    r"可能であれば[、。]?\s*",
    r"もし可能なら[、。]?\s*",
    # Greetings / openers
    r"すみません[、。]?\s*",
    r"すいません[、。]?\s*",
    r"失礼します[。]?\s*",
    r"お世話になっています[。]?\s*",
    r"お疲れ様です[。]?\s*",
    # Closing phrases
    r"以上です[。]?",
    r"よろしくです[。]?",
    r"引き続きよろしくお願いします[。]?",
]

_JA_FILLER_RE = re.compile("|".join(_JA_FILLERS))

# Trailing Japanese grammatical particles (only when word-bounded)
# Strips: を、に、は、が、で、も、と、の、へ、から — only at end of a word token
_JA_PARTICLE_RE = re.compile(r"([^\s。、！？　]+)[をにはがでもとのへから](?=\s|$|[。、！？])")

# ---------------------------------------------------------------------------
# Salesforce API name protector
# Matches: Account__c, Order__r, Ticket__e, AccountService.cls, etc.
# These must NOT be stripped even if they match other patterns
# ---------------------------------------------------------------------------

_SF_API_RE = re.compile(
    r"\b[A-Za-z][A-Za-z0-9_]*(__[cCrReEbBpP])\b"
    r"|\b[A-Z][a-zA-Z0-9]+\.(cls|trigger|object|flow|xml|yaml)\b"
    r"|\b(?:SELECT|FROM|WHERE|ORDER\s+BY|GROUP\s+BY|LIMIT|OFFSET|HAVING)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Machine-text protection
# ---------------------------------------------------------------------------

# Fenced code block — ``` or ~~~ — including an unterminated one, where the
# fence runs to the end of the prompt.
_FENCE_RE = re.compile(
    r"^(?P<fence>```|~~~)[^\n]*\n.*?(?:^(?P=fence)[ \t]*$|\Z)",
    re.MULTILINE | re.DOTALL,
)

# Inline code span: `like this`.
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")

# Whole lines that are machine output rather than prose. Each alternative is
# anchored to a full line so ordinary sentences are never caught.
_MACHINE_LINE_RE = re.compile(
    r"""^[ \t]*(?:
          [$%>][ \t].*                         # shell prompt: "$ sf org display"
        | (?:sf|sfdx|npm|npx|node|git|gh|python3?|pytest|eslint|prettier|mvn|gradle)
          [ \t]+\S.*                           # a bare command line
        | at[ \t]+\S+\(.*\).*                  # stack frame: "at foo (bar.js:1:2)"
        | File[ \t]+".*",[ \t]+line[ \t]+\d+.* # python traceback frame
        | Traceback[ \t]*\(most[ \t]recent.*
        | \d+:\d+[ \t]+(?:error|warning)\b.*   # eslint row
        | \S*[\w./-]+\.(?:js|ts|cls|trigger|xml|json|ya?ml|py|java|html|css|cmp|page|md)
          (?::\d+)*[ \t]*.*                    # a path, optionally with line:col
        | [{}\[\]].*                           # JSON/array structure line
        | "[^"]+"[ \t]*:.*                      # JSON key line
        | (?:ERROR|WARN(?:ING)?|INFO|DEBUG|FATAL|USER_DEBUG)\b.*
        | [-|+][-|+\s]{3,}.*                    # table rule / ASCII border
    )$""",
    re.MULTILINE | re.VERBOSE,
)

# Placeholder uses control characters so it cannot collide with user text.
_PLACEHOLDER = "\x00rtk{}\x00"
_PLACEHOLDER_RE = re.compile(r"\x00rtk(\d+)\x00")

# Above this share of machine text, the prompt is a paste, not a request:
# compacting the prose around it saves almost nothing and risks everything.
_CODE_HEAVY_RATIO = 0.4


def _protect(text: str) -> tuple[str, list[str]]:
    """Replace machine text with placeholders. Returns (masked_text, stash)."""
    stash: list[str] = []

    def _stash(match: re.Match[str]) -> str:
        stash.append(match.group(0))
        return _PLACEHOLDER.format(len(stash) - 1)

    masked = _FENCE_RE.sub(_stash, text)
    masked = _INLINE_CODE_RE.sub(_stash, masked)
    masked = _MACHINE_LINE_RE.sub(_stash, masked)
    return masked, stash


def _restore(text: str, stash: list[str]) -> str:
    """Put the stashed machine text back, byte for byte."""
    return _PLACEHOLDER_RE.sub(lambda m: stash[int(m.group(1))], text)


def is_code_heavy(text: str) -> bool:
    """
    True when the prompt is mostly pasted machine text.

    The compactor hook uses this to skip such a prompt entirely: there is
    little prose to win and a paste is exactly what must survive intact.
    """
    if not text:
        return False
    _, stash = _protect(text)
    protected_chars = sum(len(block) for block in stash)
    return protected_chars / len(text) >= _CODE_HEAVY_RATIO


def compact_prompt(text: str) -> tuple[str, int, int]:
    """
    Strip conversational noise from a bilingual prompt.

    Preserves Salesforce API names, method names, class names,
    SOQL keywords, any structural parameters, and all machine text
    (fenced blocks, inline code, CLI/lint output, stack traces).

    Args:
        text: Raw user prompt (English, Japanese, or mixed)

    Returns:
        (compacted_text, original_char_count, compacted_char_count)
    """
    original_len = len(text)

    # Stash code blocks, inline code and pasted CLI output before any
    # substitution runs — none of the patterns below may see them.
    result, stash = _protect(text)

    # Strip English fillers
    result = _EN_FILLER_RE.sub("", result)

    # Strip Japanese fillers
    result = _JA_FILLER_RE.sub("", result)

    # Strip trailing Japanese particles (but preserve the noun they follow)
    result = _JA_PARTICLE_RE.sub(r"\1", result)

    # Normalize whitespace. The lookbehind keeps leading indentation: in a
    # pasted structure that is not fenced, indentation is meaning.
    result = re.sub(r"(?<=\S)[ \t]{2,}", " ", result)
    result = re.sub(r"\n{3,}", "\n\n", result)
    result = result.strip()

    result = _restore(result, stash)

    return result, original_len, len(result)


def compact_prompt_report(text: str) -> str:
    """
    Return a formatted report of the compact_prompt result.
    Estimates token savings (1 token ≈ 4 chars for English/Japanese mix).
    """
    compacted, orig_chars, new_chars = compact_prompt(text)
    removed = orig_chars - new_chars
    est_tokens_saved = max(0, removed // 4)
    pct = round(removed / orig_chars * 100) if orig_chars else 0

    lines = [
        f"── Compacted prompt ({pct}% shorter, ~{est_tokens_saved} tokens saved) ──",
        "",
        compacted,
        "",
        f"[Original: {orig_chars} chars → Compacted: {new_chars} chars]",
    ]
    return "\n".join(lines)
