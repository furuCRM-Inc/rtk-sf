"""
soql_compiler.py — deterministic natural-language-to-SOQL compiler.

Ported from furuCRM-Inc/flash-agent-stack's "Jev" engine
(apps/backend/src/engine/jev-intent.ts + soqlCompiler.ts), keeping only the
fully deterministic, LLM-free layer: regex/keyword extraction of filter
conditions from free text, plus a schema-validated SOQL/SOSL string compiler.

Jev itself is a 3-tier pipeline (regex fast-route -> cheap LLM classify ->
full LLM fallback). Only tier 1 and the post-processing half of tier 2 are
LLM-free; both require a Cloudflare Workers AI binding rtk-sf has no
equivalent of. That's fine: rtk-sf's caller (Claude) already plays the role
of Jev's fallback LLM tier for free, inside the same conversation — so only
the deterministic layer needs porting here. When nothing in this module
matches, the caller falls back to get_object_schema + a hand-written
soql_query call, exactly like Jev's own LLM fallback tier.

Field/type validation reads rtk-sf's local YAML index (via
data_tools.load_object_field_rows) instead of flash-agent-stack's
Cloudflare-KV schema cache — same "zero hallucination" guarantee, different
schema source. An empty valid_fields set (object not indexed) skips
validation, matching the original TS behavior.
"""

from __future__ import annotations

import re
from typing import Any, Literal

SoqlOp = Literal[
    "eq", "neq", "gt", "gte", "lt", "lte", "like",
    "in", "not_in", "is_null", "not_null", "includes", "excludes",
]

# ---------------------------------------------------------------------------
# Synonym maps (jev-intent.ts:555-609)
# ---------------------------------------------------------------------------

STANDARD_SYNONYM_MAP: dict[str, str] = {
    # Account
    "取引先": "Account", "顧客": "Account", "会社": "Account",
    "クライアント": "Account", "法人": "Account", "企業": "Account", "得意先": "Account",
    # Contact
    "連絡先": "Contact", "取引先責任者": "Contact", "担当者": "Contact",
    "個人": "Contact", "コンタクト": "Contact",
    # Opportunity
    "商談": "Opportunity", "案件": "Opportunity", "売上": "Opportunity",
    "受注": "Opportunity", "オポチュニティ": "Opportunity", "取引": "Opportunity",
    "提案": "Opportunity",
    # Lead
    "リード": "Lead", "見込み客": "Lead", "見込み": "Lead",
    "問い合わせ": "Lead", "リード候補": "Lead", "見込客": "Lead",
    # Case
    "ケース": "Case", "チケット": "Case", "サポート": "Case",
    "サポートケース": "Case", "クレーム": "Case", "問い合わせ票": "Case",
    # Campaign
    "キャンペーン": "Campaign",
    # Task
    "行動": "Task", "タスク": "Task", "予定": "Task", "アクション": "Task",
}

FIELD_SYNONYM_MAP: dict[str, str] = {
    "フェーズ": "StageName", "ステージ": "StageName", "商談フェーズ": "StageName",
    "stage": "StageName", "phase": "StageName",
    "金額": "Amount", "予算": "Amount", "受注金額": "Amount", "案件金額": "Amount",
    "amount": "Amount", "budget": "Amount", "value": "Amount",
    "クローズ日": "CloseDate", "完了日": "CloseDate", "契約予定日": "CloseDate", "完了予定日": "CloseDate", "完了予定": "CloseDate",
    "close date": "CloseDate", "closing date": "CloseDate",
    "次のステップ": "NextStep", "ネクストステップ": "NextStep",
    "next step": "NextStep",
    "説明": "Description", "備考": "Description",
    "description": "Description", "notes": "Description",
    "担当者": "OwnerId",
    "owner": "OwnerId",
    "優先度": "Priority",
    "priority": "Priority",
    "ステータス": "Status",
    "status": "Status",
    "電話": "Phone",
    "phone": "Phone",
    "メール": "Email",
    "email": "Email",
    "ウェブサイト": "Website",
    "website": "Website",
    "phone number": "Phone", "電話番号": "Phone",
    "email address": "Email", "メールアドレス": "Email",
}

# ---------------------------------------------------------------------------
# Grammar rules (soqlCompiler.ts:34-68)
# ---------------------------------------------------------------------------

DEFAULT_GRAMMAR_RULES: dict[str, Any] = {
    "soql_rules": {
        "non_filterable_field_types": [
            "textarea", "encryptedstring", "location", "address", "base64", "multipicklist",
        ],
        "default_limit": 20,
        "max_limit": 2000,
    },
    "sosl_rules": {
        "trigger_keywords": [
            "find", "search across", "look across", "anywhere", "any object",
            "横断検索", "全体検索", "一括検索", "どこでも",
        ],
        "default_returning": ["Account", "Contact", "Opportunity", "Lead", "Case"],
    },
}

DEFAULT_SELECT_FIELDS: dict[str, list[str]] = {
    "Account": ["Id", "Name", "Type", "Industry", "AnnualRevenue", "OwnerId"],
    "Contact": ["Id", "Name", "Email", "Phone", "AccountId", "Title"],
    "Opportunity": ["Id", "Name", "Amount", "StageName", "CloseDate", "AccountId", "OwnerId"],
    "Lead": ["Id", "Name", "Company", "Email", "Phone", "Status", "LeadSource"],
    "Case": ["Id", "Subject", "Status", "Priority", "AccountId", "OwnerId"],
    "Campaign": ["Id", "Name", "Type", "Status", "StartDate", "EndDate"],
    "Task": ["Id", "Subject", "Status", "Priority", "ActivityDate", "WhoId"],
}

# rtk-sf's local index only captures fields that were explicitly customized
# (each has its own field-meta.xml) — an unmodified standard field like
# CreatedDate or Amount has no such file and is invisible to valid_fields
# even though it always exists on the object. Without this, WHERE/ORDER BY
# conditions built on these fields (including this module's own recency
# fallback) get silently stripped by compile_query's schema validation,
# quietly changing "recent accounts" into "all accounts". Only fields
# genuinely guaranteed to exist are listed here — merged in per-object so a
# field real only on Opportunity (e.g. Amount) isn't blindly trusted on Case.
STANDARD_UNIVERSAL_FIELDS: set[str] = {
    "Id", "Name", "OwnerId", "CreatedDate", "CreatedById",
    "LastModifiedDate", "LastModifiedById", "SystemModstamp", "IsDeleted",
}
STANDARD_OBJECT_FIELDS: dict[str, set[str]] = {
    "Opportunity": {"Amount", "StageName", "CloseDate", "AccountId", "IsClosed", "IsWon", "LastActivityDate"},
    "Account": {"AnnualRevenue", "Phone", "Website", "Industry", "Type", "LastActivityDate"},
    "Contact": {"Phone", "Email", "AccountId", "Title", "LastActivityDate"},
    "Lead": {"Company", "Phone", "Email", "Status", "LeadSource", "IsConverted", "LastActivityDate"},
    "Case": {"Subject", "Status", "Priority", "AccountId", "IsClosed", "LastActivityDate"},
    "Campaign": {"Type", "Status", "StartDate", "EndDate"},
    "Task": {"Subject", "Status", "Priority", "ActivityDate", "WhoId"},
}


def with_standard_fields(valid_fields: set[str], sobject: str | None) -> set[str]:
    """
    Union in fields guaranteed to exist on `sobject` even when the local
    metadata-only index never captured them. An empty valid_fields (object
    not indexed at all) is returned unchanged — that already means "skip
    validation entirely" everywhere in this module.
    """
    if not valid_fields:
        return valid_fields
    return valid_fields | STANDARD_UNIVERSAL_FIELDS | STANDARD_OBJECT_FIELDS.get(sobject or "", set())


# ---------------------------------------------------------------------------
# Date keyword extraction (jev-intent.ts:129-175)
# ---------------------------------------------------------------------------

_DATE_KEYWORD_MAP: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\btoday\b|本日|今日", re.I), "TODAY"),
    # 一昨々日/一昨日 must precede the bare 昨日 pattern — both contain "昨日"
    # as a literal substring, and this list is first-match-wins.
    (re.compile(r"一昨々日"), "LAST_N_DAYS:3"),
    (re.compile(r"一昨日"), "LAST_N_DAYS:2"),
    (re.compile(r"\byesterday\b|昨日", re.I), "YESTERDAY"),
    (re.compile(r"\btomorrow\b|明日", re.I), "TOMORROW"),
    (re.compile(r"\bthis[\s_-]?week\b|今週", re.I), "THIS_WEEK"),
    (re.compile(r"\blast[\s_-]?week\b|先週", re.I), "LAST_WEEK"),
    (re.compile(r"\bnext[\s_-]?week\b|来週|翌週", re.I), "NEXT_WEEK"),
    (re.compile(r"\bthis[\s_-]?month\b|今月|当月", re.I), "THIS_MONTH"),
    (re.compile(r"\blast[\s_-]?month\b|先月|前月", re.I), "LAST_MONTH"),
    (re.compile(r"\bnext[\s_-]?month\b|来月|翌月", re.I), "NEXT_MONTH"),
    (re.compile(r"\bthis[\s_-]?year\b|今年|本年", re.I), "THIS_YEAR"),
    (re.compile(r"\blast[\s_-]?year\b|昨年|去年", re.I), "LAST_YEAR"),
    (re.compile(r"\bnext[\s_-]?year\b|来年|翌年", re.I), "NEXT_YEAR"),
    (re.compile(r"\bthis[\s_-]?quarter\b|今四半期|今Q", re.I), "THIS_QUARTER"),
    (re.compile(r"\blast[\s_-]?quarter\b|前四半期|前Q", re.I), "LAST_QUARTER"),
    (re.compile(r"\bnext[\s_-]?quarter\b|来四半期|翌Q", re.I), "NEXT_QUARTER"),
    (re.compile(r"(?:過去|直近|last)\s*7\s*日?(?:間|days?)?", re.I), "LAST_N_DAYS:7"),
    (re.compile(r"(?:過去|直近|last)\s*14\s*日?(?:間|days?)?", re.I), "LAST_N_DAYS:14"),
    (re.compile(r"(?:過去|直近|last)\s*30\s*日?(?:間|days?)?", re.I), "LAST_N_DAYS:30"),
    (re.compile(r"(?:過去|直近|last)\s*90\s*日?(?:間|days?)?", re.I), "LAST_N_DAYS:90"),
]

_DYN_JA_DAYS = re.compile(r"(?:過去|直近)\s*(\d+)\s*日(?:間)?", re.I)
_DYN_EN_DAYS = re.compile(r"last\s+(\d+)\s+days?", re.I)


def extract_date_literal(text: str) -> str | None:
    """Extract the first matching SOQL date literal from free text."""
    dyn_ja = _DYN_JA_DAYS.search(text)
    if dyn_ja:
        return f"LAST_N_DAYS:{dyn_ja.group(1)}"
    dyn_en = _DYN_EN_DAYS.search(text)
    if dyn_en:
        return f"LAST_N_DAYS:{dyn_en.group(1)}"
    for pattern, literal in _DATE_KEYWORD_MAP:
        if pattern.search(text):
            return literal
    return None


# ---------------------------------------------------------------------------
# Amount extraction (jev-intent.ts:180-236)
# ---------------------------------------------------------------------------

_FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")
_UNIT_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(億|千万|万)")
_MILLION_RE = re.compile(r"[$￥]?\s*(\d[\d,]*(?:\.\d+)?)\s*(?:million|m)\b", re.I)
_K_RE = re.compile(r"[$￥]?\s*(\d[\d,]*(?:\.\d+)?)\s*k\b", re.I)
_PLAIN_RE = re.compile(r"[$￥]?\s*(\d[\d,]{2,})(?:\s*(?:円|ドル|USD|JPY))?")


def extract_amount_value(text: str) -> float | None:
    """Extract a numeric amount, handling JA units (万/千万/億, incl. compounds) and EN magnitude words."""
    normalized = text.translate(_FULLWIDTH_DIGITS).replace("，", ",")

    # Compound Japanese numerals: "1億5000万" sums contiguous 億/千万/万
    # components left to right instead of only taking the first match.
    compound_sum: float | None = None
    last_end = -1
    for m in _UNIT_RE.finditer(normalized):
        if compound_sum is not None and m.start() - last_end > 2:
            break
        raw = float(m.group(1).replace(",", ""))
        unit = m.group(2)
        mult = 100_000_000 if unit == "億" else 10_000_000 if unit == "千万" else 10_000
        compound_sum = (compound_sum or 0) + raw * mult
        last_end = m.end()
    if compound_sum is not None:
        return compound_sum

    million_match = _MILLION_RE.search(normalized)
    if million_match:
        return float(million_match.group(1).replace(",", "")) * 1_000_000
    k_match = _K_RE.search(normalized)
    if k_match:
        return float(k_match.group(1).replace(",", "")) * 1_000

    plain_match = _PLAIN_RE.search(normalized)
    if plain_match:
        v = float(plain_match.group(1).replace(",", ""))
        if v >= 1000:
            return v

    return None


def extract_amount_op(text: str) -> SoqlOp:
    """Detect a threshold operator (以上/以下/超/未満/above/below/over/under/exactly)."""
    if re.search(r"(?:ちょうど|exactly|equal\s+to)", text, re.I):
        return "eq"
    if re.search(r"(?:以上|超過?|above|over|more\s+than|>=)", text, re.I):
        return "gte"
    if re.search(r"(?:以下|未満|below|under|less\s+than|<=)", text, re.I):
        return "lte"
    if re.search(r"(?:超|より多い|>(?!=))", text, re.I):
        return "gt"
    if re.search(r"(?:未満|より少ない|<(?!=))", text, re.I):
        return "lt"
    return "gte"


_ALL_RECORDS_LIMIT = 200
_MAX_PRACTICAL_LIMIT = 100
_LIMIT_COUNT_RE = re.compile(r"(\d+)\s*(?:件|records?|rows?)", re.I)


def extract_limit(text: str) -> int:
    if re.search(r"\b(?:all|every)\b|すべて|全て", text, re.I):
        return _ALL_RECORDS_LIMIT
    m = _LIMIT_COUNT_RE.search(text)
    if m:
        n = int(m.group(1))
        if n >= 1:
            return min(n, _MAX_PRACTICAL_LIMIT)
    return 20


def _escape_like_wildcards(value: str) -> str:
    return re.sub(r"[%_]", lambda mo: "\\" + mo.group(0), value)


_PARENT_ACCOUNT_PATTERNS = [
    re.compile(r"(.+?)取引先の(?:全て|すべて)?の?(?:商談|案件|売上|受注|オポチュニティ)"),
    re.compile(r"(.+?)という取引先の(?:全て|すべて)?の?(?:商談|案件)"),
    re.compile(r"(.+?)(?:'s|’s)\s+(?:opportunit(?:y|ies)|deals?)", re.I),
]
_PARENT_ACCOUNT_TRAILING = re.compile(
    r"(?:opportunit(?:y|ies)|deals?)\s+(?:for|at|from|related\s+to)\s+(.+?)(?:\s*$|\.$)", re.I
)


def extract_parent_account_context(text: str) -> str | None:
    """Detect "X取引先の商談" / "X's opportunities" / "opportunities for X" — returns the (escaped) account name."""
    for pattern in _PARENT_ACCOUNT_PATTERNS:
        m = pattern.search(text)
        if m:
            name = m.group(1).strip()
            if len(name) >= 2:
                return _escape_like_wildcards(name)
    m = _PARENT_ACCOUNT_TRAILING.search(text)
    if m:
        name = m.group(1).strip()
        if len(name) >= 2:
            return _escape_like_wildcards(name)
    return None


def extract_stage_filter(text: str) -> dict[str, Any] | None:
    if re.search(r"(?:クローズ済|closed\s+won|受注|Closed Won)", text, re.I):
        return {"field": "StageName", "op": "eq", "value": "Closed Won"}
    if re.search(r"(?:失注|Closed Lost|lost)", text, re.I):
        return {"field": "StageName", "op": "eq", "value": "Closed Lost"}
    if re.search(r"(?:進行中|オープン|open|active|未クローズ)", text, re.I):
        return {"field": "IsClosed", "op": "eq", "value": False}
    return None


def extract_case_status_filter(text: str) -> dict[str, Any] | None:
    # Negated-closed phrasing must be checked FIRST — "クローズしていない" contains
    # the bare substring "クローズ", which the closed-branch regex below matches on
    # its own, silently inverting the user's actual (negative) intent.
    if re.search(r"クローズ(?:して)?(?:い)?ない|not\s+closed|isn'?t\s+closed", text, re.I):
        return {"field": "IsClosed", "op": "eq", "value": False}
    if re.search(r"\bclosed?\b|クローズ済?|解決済?|完了したケース?", text):
        return {"field": "IsClosed", "op": "eq", "value": True}
    if re.search(r"\bopen\b|未解決|未クローズ|オープンなケース?", text):
        return {"field": "IsClosed", "op": "eq", "value": False}
    return None


def extract_lead_status_filter(text: str) -> dict[str, Any] | None:
    if re.search(r"未変換|変換されていない|not\s+converted|unconverted", text, re.I):
        return {"field": "IsConverted", "op": "eq", "value": False}
    if re.search(r"変換済み?|converted", text, re.I):
        return {"field": "IsConverted", "op": "eq", "value": True}
    return None


_NULL_JA_RE = re.compile(r"(.+?)が(?:未設定|空欄|未入力|ない|無い)")
_NULL_EN_RE = re.compile(r"\b(?:no|without(?:\s+an?)?|missing)\s+([a-zA-Z][a-zA-Z\s]*)", re.I)
_NULL_EN_TRAILING = re.compile(
    r"\s+(?:leads?|accounts?|contacts?|cases?|opportunit(?:y|ies))\s*$", re.I
)


def extract_null_check_filter(text: str, valid_fields: set[str]) -> dict[str, Any] | None:
    """Detect "no X" / "without X" / "Xが未設定/空欄/未入力/ない" -> IS NULL condition."""
    field_raw: str | None = None
    ja_match = _NULL_JA_RE.search(text)
    en_match = None if ja_match else _NULL_EN_RE.search(text)

    if ja_match:
        field_raw = ja_match.group(1).strip()
    elif en_match:
        field_raw = _NULL_EN_TRAILING.sub("", en_match.group(1)).strip().lower()

    if not field_raw:
        return None

    api_field = FIELD_SYNONYM_MAP.get(field_raw)
    if not api_field:
        return None
    if valid_fields and api_field not in valid_fields:
        return None

    return {"field": api_field, "op": "is_null"}


# ---------------------------------------------------------------------------
# Main SOQL filter builder (jev-intent.ts:406-550)
# ---------------------------------------------------------------------------


def build_soql_filter(
    user_input: str,
    sobject_hint: str | None,
    valid_fields: set[str],
) -> dict[str, Any]:
    """
    Build a deterministic SOQL filter from free text via keyword extraction.

    Unlike buildSoqlFilterFromJev, there are no Jev noul-confidence answers to
    gate on here (no LLM call happened) — each extractor's own match success
    is the inclusion signal, which has the same practical effect.

    Returns {"conditions": [...], "order_by": ..., "limit": ...}.
    """
    sobject = sobject_hint or "Account"
    valid_fields = with_standard_fields(valid_fields, sobject)
    conditions: list[dict[str, Any]] = []

    # Parent-account context: "X取引先の商談" -> Account.Name filter
    parent_account = extract_parent_account_context(user_input)
    if parent_account:
        conditions.append({"field": "Account.Name", "op": "like", "value": f"%{parent_account}%"})

    # Date filter
    date_literal = extract_date_literal(user_input)
    if date_literal:
        if sobject == "Opportunity":
            lower = user_input.lower()
            is_created_intent = bool(re.search(r"作成|新規|追加|created|added|\bnew\b", lower))
            date_field = "CreatedDate" if is_created_intent else "CloseDate"
        else:
            date_field = "CreatedDate"
        date_op: SoqlOp = (
            "eq"
            if re.match(r"^(?:TODAY|YESTERDAY|TOMORROW|THIS_|LAST_WEEK$|LAST_MONTH$|LAST_QUARTER$|LAST_YEAR$|NEXT_)", date_literal)
            else "gte"
        )
        if not valid_fields or date_field in valid_fields:
            conditions.append({"field": date_field, "op": date_op, "value": date_literal})

    # Amount filter (Opportunity / Account)
    if sobject in ("Opportunity", "Account"):
        amount_val = extract_amount_value(user_input)
        if amount_val is not None:
            amount_field = "Amount" if sobject == "Opportunity" else "AnnualRevenue"
            if not valid_fields or amount_field in valid_fields:
                conditions.append({"field": amount_field, "op": extract_amount_op(user_input), "value": amount_val})

    # Inactive / stale records
    is_inactive = bool(re.search(
        r"neglect|inactiv|stale|overdue|放置|停滞|未活動|活動なし", user_input, re.I
    ))
    if is_inactive:
        if not valid_fields or "LastActivityDate" in valid_fields:
            conditions.append({"field": "LastActivityDate", "op": "lt", "value": "LAST_N_DAYS:14"})
        if sobject == "Opportunity" and (not valid_fields or "IsClosed" in valid_fields):
            conditions.append({"field": "IsClosed", "op": "eq", "value": False})

    # Stage filter (Opportunity only)
    if sobject == "Opportunity":
        stage_cond = extract_stage_filter(user_input)
        if stage_cond and (not valid_fields or stage_cond["field"] in valid_fields):
            conditions.append(stage_cond)

    # Status filter (Case / Lead)
    if sobject == "Case":
        case_status = extract_case_status_filter(user_input)
        if case_status and (not valid_fields or case_status["field"] in valid_fields):
            conditions.append(case_status)
    if sobject == "Lead":
        lead_status = extract_lead_status_filter(user_input)
        if lead_status and (not valid_fields or lead_status["field"] in valid_fields):
            conditions.append(lead_status)

    # Null/blank field filter ("no phone number", "電話番号が未設定")
    null_check = extract_null_check_filter(user_input, valid_fields)
    if null_check:
        conditions.append(null_check)

    # Recency fallback (no explicit condition matched)
    if not conditions:
        if not valid_fields or "CreatedDate" in valid_fields:
            conditions.append({"field": "CreatedDate", "op": "gte", "value": "LAST_N_DAYS:30"})

    order_by = (
        "LastActivityDate ASC" if is_inactive
        else "Amount DESC" if sobject == "Opportunity"
        else "CreatedDate DESC"
    )

    return {"conditions": conditions, "order_by": order_by, "limit": extract_limit(user_input)}


# ---------------------------------------------------------------------------
# RECORD_UPDATE extractor (jev-intent.ts:622-705)
# ---------------------------------------------------------------------------

_UPDATE_JA_RE = re.compile(
    r"^(.+?)(?:の|：|:)\s*(.+?)\s*を\s*(.+?)\s*(?:に変更|に設定|に更新|に修正|として保存|にして|に直して|にする)", re.I
)
_UPDATE_JA_SIMPLE_RE = re.compile(
    r"^(.+?)\s*を\s*(.+?)\s*(?:に変更|に設定|に更新|に修正|として保存|にして|に直して|にする)", re.I
)
_UPDATE_EN_RE = re.compile(
    r"^(?:change|set|update)\s+(.+?)(?:'s|’s)\s+(.+?)\s+to\s+(.+?)\.?$", re.I
)
_UPDATE_EN_SIMPLE_RE = re.compile(
    r"^(?:change|set|update)\s+(?:the\s+)?(.+?)\s+to\s+(.+?)\.?$", re.I
)


def extract_simple_update(
    user_input: str,
    sobject_hint: str | None,
    valid_fields: set[str],
) -> dict[str, Any] | None:
    """Parse "[record] の [field] を [value] に変更" / "change [record]'s [field] to [value]"."""
    ja_match = _UPDATE_JA_RE.match(user_input)
    ja_simple = None if ja_match else _UPDATE_JA_SIMPLE_RE.match(user_input)
    en_match = None if (ja_match or ja_simple) else _UPDATE_EN_RE.match(user_input)
    en_simple = None if (ja_match or ja_simple or en_match) else _UPDATE_EN_SIMPLE_RE.match(user_input)

    if not (ja_match or ja_simple or en_match or en_simple):
        return None

    search_name: str | None = None

    if ja_match:
        candidate = ja_match.group(1).strip()
        field_raw = ja_match.group(2).strip()
        value_raw = ja_match.group(3).strip()
        if candidate not in FIELD_SYNONYM_MAP and candidate not in valid_fields:
            search_name = candidate
        else:
            field_raw = candidate
    elif en_match:
        candidate = en_match.group(1).strip()
        field_raw = en_match.group(2).strip()
        value_raw = en_match.group(3).strip()
        if candidate.lower() not in FIELD_SYNONYM_MAP and candidate not in valid_fields:
            search_name = candidate
        else:
            field_raw = candidate
    elif ja_simple:
        field_raw = ja_simple.group(1).strip()
        value_raw = ja_simple.group(2).strip()
    else:
        assert en_simple is not None
        field_raw = en_simple.group(1).strip()
        value_raw = en_simple.group(2).strip()

    api_field = FIELD_SYNONYM_MAP.get(field_raw) or FIELD_SYNONYM_MAP.get(field_raw.lower()) or field_raw

    if valid_fields and api_field not in valid_fields:
        return None

    parsed_value: Any = value_raw
    if re.search(r"amount|revenue|金額|予算", api_field, re.I):
        amount_val = extract_amount_value(value_raw)
        if amount_val is not None:
            parsed_value = amount_val
    elif re.search(r"date|日", api_field, re.I):
        date_lit = extract_date_literal(value_raw)
        if date_lit:
            parsed_value = date_lit

    return {"fields": {api_field: parsed_value}, "sobject": sobject_hint, "search_name": search_name}


# ---------------------------------------------------------------------------
# Fast-route intent detection (adapted from cf-worker.ts tryFastRoute)
# ---------------------------------------------------------------------------
#
# tryFastRoute in the original only detects SOQL_SEARCH/NAVIGATE/SEARCH/
# GUIDE_CREATE — RECORD_UPDATE is only ever reached after an LLM (Jev or the
# full fallback) has already classified intent, and extract_simple_update()
# just parses assuming that intent is already known. Since rtk-sf has no LLM
# tier of its own, this checks the SOQL_SEARCH regexes first (same priority
# order as the original, including the NAVIGATE guard that prevents "show me
# Acme Corp" from being misread as a search), then tries
# extract_simple_update() as the RECORD_UPDATE detector.

_SOQL_RECENT_RE = re.compile(
    r"^show(?:\s+me)?\s+(?:recent(?:ly)?(?:\s+created)?|latest|new|today'?s?|"
    r"this\s+week'?s?|this\s+month'?s?|last\s+week'?s?|last\s+month'?s?)\s+"
    r"(opportunit(?:ies|y)|accounts?|contacts?|leads?|cases?)",
    re.I,
)
_CASE_STATUS_RE = re.compile(
    r"^(?:show(?:\s+me)?|list(?:\s+all)?|how\s+many)\s+(closed|open)\s+(?:cases?|tickets?)", re.I
)
_NAV_RE = re.compile(r"^(?:go\s+to|open|navigate\s+to)\s+(.+)", re.I)
_NAV_GUARD_RE = re.compile(
    r"\b(?:recent|latest|new|today|this\s+week|this\s+month|last\s+week|last\s+month|over|above|below|open|closed)\b",
    re.I,
)
_SOBJECT_PLURAL_TO_SINGULAR = {
    "opportunities": "Opportunity", "opportunity": "Opportunity",
    "accounts": "Account", "account": "Account",
    "contacts": "Contact", "contact": "Contact",
    "leads": "Lead", "lead": "Lead",
    "cases": "Case", "case": "Case",
}
_SEARCH_VERB_RE = re.compile(
    r"\b(?:show(?:\s+me)?|list(?:\s+all)?|find|search(?:\s+for)?|display|how\s+many)\b"
    r"|表示|見せて|検索|一覧|教えて|抽出",
    re.I,
)


def _has_extractable_signal(text: str, sobject: str) -> bool:
    """True when at least one condition extractor finds something concrete, independent of a search verb."""
    if extract_date_literal(text) or extract_null_check_filter(text, set()):
        return True
    if sobject in ("Opportunity", "Account") and extract_amount_value(text) is not None:
        return True
    if sobject == "Opportunity" and extract_stage_filter(text):
        return True
    if sobject == "Case" and extract_case_status_filter(text):
        return True
    if sobject == "Lead" and extract_lead_status_filter(text):
        return True
    if sobject == "Opportunity" and extract_parent_account_context(text):
        return True
    return False


def try_fast_route(user_input: str, sobject_hint: str | None = None, valid_fields: set[str] | None = None) -> dict[str, Any]:
    """
    Zero-regex-miss classification for the two intents rtk-sf can act on.

    Returns {"intent": "SOQL_SEARCH", "sobject": ..., "filter": {...}} or
    {"intent": "RECORD_UPDATE", ...extract_simple_update() result} or
    {"intent": "UNKNOWN"} — the caller (Claude) should fall back to
    get_object_schema + a hand-written soql_query call on UNKNOWN.
    """
    text = user_input.strip()
    if not text:
        return {"intent": "UNKNOWN"}
    lower = text.lower()
    valid_fields = valid_fields or set()

    soql_recent = _SOQL_RECENT_RE.match(lower)
    if soql_recent:
        sobj = _SOBJECT_PLURAL_TO_SINGULAR.get(soql_recent.group(1).lower(), sobject_hint or "Account")
        time_lit = (
            "THIS_WEEK" if re.search(r"this\s+week", lower)
            else "THIS_MONTH" if re.search(r"this\s+month", lower)
            else "LAST_WEEK" if re.search(r"last\s+week", lower)
            else "LAST_MONTH" if re.search(r"last\s+month", lower)
            else "TODAY" if re.search(r"today", lower)
            else "LAST_N_DAYS:30"
        )
        return {
            "intent": "SOQL_SEARCH",
            "sobject": sobj,
            "filter": {
                "conditions": [{"field": "CreatedDate", "op": "gte", "value": time_lit}],
                "order_by": "CreatedDate DESC",
                "limit": 20,
            },
        }

    case_status = _CASE_STATUS_RE.match(lower)
    if case_status:
        is_closed = case_status.group(1).lower() == "closed"
        return {
            "intent": "SOQL_SEARCH",
            "sobject": "Case",
            "filter": {
                "conditions": [{"field": "IsClosed", "op": "eq", "value": is_closed}],
                "order_by": "CreatedDate DESC",
                "limit": 20,
            },
        }

    # NAVIGATE guard — "show me <specific name>" / "go to/open/navigate to X"
    # has no rtk-sf analog (no UI to navigate); returning UNKNOWN here mirrors
    # the original's priority order and stops it being misread as a search.
    if _NAV_RE.match(lower):
        return {"intent": "UNKNOWN"}
    if lower.startswith("show me ") and not _NAV_GUARD_RE.search(lower):
        return {"intent": "UNKNOWN"}

    # Broader SOQL_SEARCH detection — the original only reaches
    # buildSoqlFilterFromJev (the full amount/date/stage/status/null-check
    # extractor) after an LLM has already classified intent as SOQL_SEARCH.
    # With no LLM tier, this is gated on a resolvable sObject plus either an
    # explicit search verb OR one of the extractors itself finding a concrete
    # signal (date/amount/stage/status/null-check/parent-account) — otherwise
    # arbitrary unrelated text would fall through to build_soql_filter()'s
    # always-produces-a-condition recency default and look falsely confident.
    if sobject_hint and (_SEARCH_VERB_RE.search(text) or _has_extractable_signal(text, sobject_hint)):
        return {
            "intent": "SOQL_SEARCH",
            "sobject": sobject_hint,
            "filter": build_soql_filter(text, sobject_hint, valid_fields),
        }

    update = extract_simple_update(text, sobject_hint, valid_fields)
    if update:
        return {"intent": "RECORD_UPDATE", **update}

    return {"intent": "UNKNOWN"}


# ---------------------------------------------------------------------------
# SOQL/SOSL compiler (soqlCompiler.ts)
# ---------------------------------------------------------------------------

_DATE_LITERAL_RE = re.compile(r"^(YESTERDAY|TODAY|TOMORROW|THIS_|LAST_|NEXT_)[A-Z_]+(:\d+)?$")
_DATE_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}:\d{2}Z)?$")


def _is_date_literal(s: str) -> bool:
    return bool(_DATE_LITERAL_RE.match(s) or _DATE_ISO_RE.match(s))


def _format_value(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str) and _is_date_literal(v):
        return v
    if isinstance(v, str):
        return "'" + v.replace("'", "\\'") + "'"
    return str(v)


def _op_to_soql(op: str, value: Any) -> str:
    if op == "eq":
        return f"= {_format_value(value)}"
    if op == "neq":
        return f"!= {_format_value(value)}"
    if op == "gt":
        return f"> {_format_value(value)}"
    if op == "gte":
        return f">= {_format_value(value)}"
    if op == "lt":
        return f"< {_format_value(value)}"
    if op == "lte":
        return f"<= {_format_value(value)}"
    if op == "like":
        return f"LIKE {_format_value(value)}"
    if op == "in":
        return "IN (" + ", ".join(_format_value(v) for v in value) + ")"
    if op == "not_in":
        return "NOT IN (" + ", ".join(_format_value(v) for v in value) + ")"
    if op == "is_null":
        return "= null"
    if op == "not_null":
        return "!= null"
    if op == "includes":
        return "INCLUDES (" + ", ".join(f"'{v}'" for v in value) + ")"
    if op == "excludes":
        return "EXCLUDES (" + ", ".join(f"'{v}'" for v in value) + ")"
    return f"= {_format_value(value)}"


def _resolve_select_fields(
    sobject: str,
    requested: list[str] | None,
    parent_fields: list[str] | None,
    valid_fields: set[str],
    warnings: list[str],
) -> list[str]:
    candidates = requested if requested else DEFAULT_SELECT_FIELDS.get(sobject, ["Id", "Name"])

    safe: list[str] = []
    for f in candidates:
        if valid_fields and f not in valid_fields:
            warnings.append(f'SELECT field "{f}" not in schema — omitted')
            continue
        safe.append(f)

    if "Id" not in safe:
        safe.insert(0, "Id")
    if "Name" not in safe and (not valid_fields or "Name" in valid_fields):
        safe.insert(1, "Name")

    for pf in (parent_fields or []):
        if pf not in safe:
            safe.append(pf)

    return safe


def _build_where_clause(
    conditions: list[dict[str, Any]],
    valid_fields: set[str],
    field_types: dict[str, Any],
    grammar: dict[str, Any],
    warnings: list[str],
) -> str:
    parts: list[str] = []
    non_filterable = grammar["soql_rules"]["non_filterable_field_types"]

    for cond in conditions:
        field = cond["field"]
        is_cross_object = "." in field
        if not is_cross_object and valid_fields and field not in valid_fields:
            warnings.append(f'WHERE field "{field}" not in schema — condition skipped')
            continue

        raw_ftype = field_types.get(field, "")
        ftype = (raw_ftype if isinstance(raw_ftype, str) else raw_ftype.get("type", "")).lower()
        if ftype and any(t in ftype for t in non_filterable):
            warnings.append(f'Field "{field}" (type: {ftype}) cannot be filtered — condition skipped')
            continue

        op = cond["op"]
        if ftype == "multipicklist":
            if op == "eq":
                op = "includes"
            elif op == "neq":
                op = "excludes"

        parts.append(f"{field} {_op_to_soql(op, cond.get('value'))}")

    return f"WHERE {' AND '.join(parts)}" if parts else ""


def _resolve_order_by(order_by: str | None, valid_fields: set[str], warnings: list[str]) -> str:
    if not order_by:
        return ""
    tokens = order_by.strip().split()
    raw_field = tokens[0]
    direction = tokens[1] if len(tokens) > 1 else "DESC"
    safe_dir = direction.upper() if direction.upper() in ("ASC", "DESC") else "DESC"

    if valid_fields and raw_field not in valid_fields:
        warnings.append(f'ORDER BY field "{raw_field}" not in schema — using CreatedDate DESC')
        return "ORDER BY CreatedDate DESC"
    return f"ORDER BY {raw_field} {safe_dir}"


def _resolve_limit(limit: int | None, grammar: dict[str, Any]) -> int:
    n = limit if limit is not None else grammar["soql_rules"]["default_limit"]
    safe = n if n > 0 else grammar["soql_rules"]["default_limit"]
    return min(safe, grammar["soql_rules"]["max_limit"])


def _build_sosl_query(soql_input: dict[str, Any], grammar: dict[str, Any], warnings: list[str]) -> dict[str, Any]:
    term = re.sub(r"[{}'\"`\\]", "", soql_input.get("sosl_term") or "")
    search_group = soql_input.get("sosl_group") or "ALL FIELDS"
    returning_objects = soql_input.get("returning_objects") or grammar["sosl_rules"]["default_returning"]
    returning = ", ".join(f"{obj}(Id, Name)" for obj in returning_objects)
    lim = _resolve_limit(soql_input.get("limit"), grammar)

    if not term.strip():
        warnings.append("SOSL search term is empty — query may return no results")

    query = f"FIND {{{term}}} IN {search_group} RETURNING {returning} LIMIT {lim}"
    return {"query_type": "SOSL", "query": query, "warnings": warnings}


def should_use_sosl(user_input: str, sobject: str | None, grammar: dict[str, Any] = DEFAULT_GRAMMAR_RULES) -> bool:
    if not sobject:
        return True
    lower = user_input.lower()
    return any(kw.lower() in lower for kw in grammar["sosl_rules"]["trigger_keywords"])


def compile_query(
    soql_input: dict[str, Any],
    valid_fields: set[str] | None = None,
    field_types: dict[str, Any] | None = None,
    grammar: dict[str, Any] = DEFAULT_GRAMMAR_RULES,
) -> dict[str, Any]:
    """
    Compile a typed intent payload into a validated SOQL or SOSL string.

    soql_input keys: intent ("SOQL_SEARCH"|"SOSL_SEARCH"), sobject, conditions,
    select_fields, parent_fields, order_by, limit, sosl_term, sosl_group,
    returning_objects. Child-relationship subqueries are not supported (see
    module-level docstring) — rtk-sf's indexer doesn't capture relationshipName.

    valid_fields empty = skip field-existence validation (matches the
    original TS's degrade-gracefully behavior for an unindexed object).
    """
    valid_fields = valid_fields or set()
    field_types = field_types or {}
    warnings: list[str] = []

    if soql_input.get("intent") == "SOSL_SEARCH" or (not soql_input.get("sobject") and soql_input.get("sosl_term")):
        return _build_sosl_query(soql_input, grammar, warnings)

    sobject = soql_input.get("sobject")
    if not sobject:
        return {"query_type": "SOQL", "query": "", "warnings": ["sObject is required for SOQL"]}
    valid_fields = with_standard_fields(valid_fields, sobject)

    select_fields = _resolve_select_fields(
        sobject, soql_input.get("select_fields"), soql_input.get("parent_fields"), valid_fields, warnings
    )
    where_clause = _build_where_clause(soql_input.get("conditions", []), valid_fields, field_types, grammar, warnings)
    order_by = _resolve_order_by(soql_input.get("order_by"), valid_fields, warnings)
    limit = _resolve_limit(soql_input.get("limit"), grammar)

    parts = [
        f"SELECT {', '.join(select_fields)}",
        f"FROM {sobject}",
        where_clause,
        order_by,
        f"LIMIT {limit}",
        "WITH USER_MODE",  # always enforce FLS + sharing
    ]
    query = " ".join(p for p in parts if p)

    return {"query_type": "SOQL", "query": query, "warnings": warnings}
