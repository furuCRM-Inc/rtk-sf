"""
search.py — SQLite FTS5 + optional vector hybrid search engine.

Provides keyword search over indexed Salesforce metadata specs using SQLite's
built-in FTS5 full-text search. When numpy is available, cosine similarity
is layered on top for semantic ranking.

The index uses the trigram tokenizer, which has two consequences that the
query layer has to compensate for — both of which made CJK queries look
broken:

  * A term shorter than 3 characters matches *nothing*, silently. Plenty of
    Japanese words are 2 characters ("職員", "番号", "商談"), so such a term
    would wipe out the whole result set.
  * FTS5 joins bare terms with an implicit AND, so a multi-word query like
    "セルフ登録 職員番号" only matches a component containing both strings
    verbatim — 0 results as soon as the words live in sibling components.

`search()` therefore widens progressively (AND → OR → LIKE) and ranks by how
many of the query terms a component actually matched.

Usage:
    engine = SearchEngine("/path/to/project")
    results = engine.search("AccountService", limit=5)
    spec = engine.get_spec("AccountService")
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import unicodedata
from pathlib import Path
from typing import Any, NamedTuple

logger = logging.getLogger(__name__)

RTK_DIR = ".rtk-sf"
DB_FILE = "db.sqlite"
SPECS_DIR = "specs"
RELATIONS_FILE = "relations.json"


# ---------------------------------------------------------------------------
# Optional vector support
# ---------------------------------------------------------------------------

try:
    import numpy as np  # type: ignore

    _NUMPY_AVAILABLE = True
except ImportError:
    _NUMPY_AVAILABLE = False
    logger.debug("numpy not available; falling back to FTS5-only search.")


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two float vectors."""
    if not _NUMPY_AVAILABLE:
        return 0.0
    va = np.array(a, dtype=float)
    vb = np.array(b, dtype=float)
    denom = np.linalg.norm(va) * np.linalg.norm(vb)
    if denom == 0:
        return 0.0
    return float(np.dot(va, vb) / denom)


def _bag_of_words_vector(text: str, vocab: list[str]) -> list[float]:
    """
    Create a simple bag-of-words vector from text using a vocabulary list.

    This is a lightweight stand-in for proper embeddings when an LLM API is
    not available, preserving the zero-external-API constraint of rtk-sf.

    CJK vocabulary entries are matched as substrings rather than whitespace
    tokens: Japanese text carries no spaces, so "職員番号を更新" would never
    equal the token "職員番号" and every cosine score came out 0.
    """
    lowered = text.lower()
    words = set(lowered.split())
    return [
        1.0 if (term in words or (_CJK_RE.search(term) and term in lowered)) else 0.0
        for term in vocab
    ]


# ---------------------------------------------------------------------------
# Query analysis
# ---------------------------------------------------------------------------

# Hiragana, katakana, CJK ideographs, halfwidth katakana.
_CJK_RE = re.compile(
    r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff66-\uff9f]"
)

# Explicit FTS5 syntax — such a query is handed to FTS5 verbatim so power
# users keep prefix (`Account*`), phrase ("…") and boolean operators.
_FTS_SYNTAX_RE = re.compile(r'["*():]|(?:^|\s)(?:AND|OR|NOT|NEAR)(?:\s|$)')

# Term separators: ASCII whitespace plus the Japanese ideographic space and
# the punctuation people actually type between keywords.
_TERM_SPLIT_RE = re.compile(r"[\s\u3000、，,;；/|]+")

# Characters stripped from the edges of a term — Japanese sentence enders and
# brackets carry no search value.
_TERM_TRIM = "。．.!！?？:：\"'「」『』（）()[]【】<>"

# The trigram tokenizer cannot index or match anything shorter than this.
_TRIGRAM_MIN = 3


def _normalize(text: str) -> str:
    """NFKC-fold and lowercase — collapses full-width ＡＢＣ/１２３ and ｶﻨ forms."""
    return unicodedata.normalize("NFKC", text).lower()


def _query_terms(query: str) -> list[str]:
    """Split a query into search terms, preserving order and dropping noise."""
    terms: list[str] = []
    for chunk in _TERM_SPLIT_RE.split(query.strip()):
        term = chunk.strip(_TERM_TRIM)
        if term:
            terms.append(term)
    return terms


def _term_variants(term: str) -> list[str]:
    """
    Return the forms of *term* worth matching against the index.

    The index stores raw (lowercased) spec text, so a full-width query term
    has to be tried in both its original and NFKC-folded form rather than
    forcing a re-index.
    """
    return list(dict.fromkeys([term.lower(), _normalize(term)]))


def _fts_group(variants: list[str]) -> str:
    """Build an FTS5 sub-expression matching any variant of one term."""
    phrases = ['"' + v.replace('"', '""') + '"' for v in variants]
    return phrases[0] if len(phrases) == 1 else "(" + " OR ".join(phrases) + ")"


def _is_trigram_searchable(variants: list[str]) -> bool:
    """True if at least one variant is long enough for the trigram index."""
    return any(len(v) >= _TRIGRAM_MIN for v in variants)


# ---------------------------------------------------------------------------
# Japanese ↔ English vocabulary
# ---------------------------------------------------------------------------

# Salesforce components are named in English; Japanese projects describe them
# in Japanese. Nothing connects the two, so a query of
# "セルフ登録" could not reach `SelfRegistrationController` while "register"
# could. Same idea as soql_compiler.FIELD_SYNONYM_MAP: a curated table, no
# model call. A project adds its own in `.rtk-sf/synonyms.yaml`:
#
#     セルフ登録: [selfRegistration, SelfRegistrationController]
#
_BUILTIN_SYNONYMS: dict[str, tuple[str, ...]] = {
    # Processes
    "セルフ登録": ("selfregistration", "self registration", "selfregist", "register"),
    "自己登録": ("selfregistration", "self registration", "register"),
    "登録": ("register", "registration", "create"),
    "申込": ("application", "apply", "entry"),
    "申請": ("application", "request"),
    "承認": ("approval", "approve"),
    "更新": ("update", "refresh"),
    "削除": ("delete", "remove"),
    "検索": ("search", "query", "find"),
    "一覧": ("list", "listview", "table"),
    "画面": ("screen", "page", "component", "view"),
    "帳票": ("report", "document", "pdf"),
    "添付": ("attachment", "file", "contentdocument"),
    "ログイン": ("login", "signin"),
    "認証": ("auth", "authentication"),
    "権限": ("permission", "access"),
    "権限セット": ("permissionset", "permission set"),
    # People and attributes
    "職員": ("staff", "employee"),
    "職員番号": ("staffnumber", "staff number", "employeenumber", "employee number"),
    "社員": ("employee", "staff"),
    "氏名": ("name", "fullname"),
    "名前": ("name",),
    "生年月日": ("birthdate", "birth date", "dateofbirth", "date of birth"),
    "年齢": ("age",),
    "住所": ("address", "street"),
    "電話番号": ("phone", "phonenumber"),
    "メールアドレス": ("email", "emailaddress"),
    "部署": ("department", "division"),
    "担当者": ("owner", "assignee", "contact"),
    "有資格者": ("qualified", "eligible", "certified"),
    "資格": ("qualification", "license", "certification"),
    "試験": ("exam", "test"),
    "受験票": ("examticket", "exam ticket"),
    # Standard objects
    "取引先": ("account",),
    "取引先責任者": ("contact",),
    "商談": ("opportunity",),
    "見込客": ("lead",),
    "リード": ("lead",),
    "ケース": ("case",),
    "問い合わせ": ("case", "inquiry"),
    "契約": ("contract",),
    "注文": ("order",),
    "商品": ("product", "product2"),
    "見積": ("quote",),
    "請求書": ("invoice",),
    "ユーザ": ("user",),
    "ユーザー": ("user",),
    "キャンペーン": ("campaign",),
    "ToDo": ("task",),
    "活動": ("activity", "task", "event"),
    # Generic nouns that still carry signal in an identifier
    "番号": ("number", "no", "code"),
    "日付": ("date",),
    "金額": ("amount", "price"),
    "数量": ("quantity",),
    "状態": ("status", "state"),
    "種別": ("type", "recordtype"),
    "区分": ("type", "category"),
    "設定": ("setting", "config"),
    "履歴": ("history", "log"),
}

SYNONYMS_FILE = "synonyms.yaml"


def _build_synonym_index(table: dict[str, tuple[str, ...]]) -> dict[str, tuple[str, ...]]:
    """
    Lowercase the table and add the reverse direction.

    Both directions matter: a Japanese query has to reach English identifiers,
    and an English query has to reach Japanese labels.
    """
    index: dict[str, set[str]] = {}
    for source, targets in table.items():
        key = source.lower()
        lowered = [t.lower() for t in targets]
        index.setdefault(key, set()).update(lowered)
        for target in lowered:
            # Reverse direction, plus the other targets of the same source:
            # they are forms of one word, so "register" has to reach
            # "selfregistration" the way "セルフ登録" does.
            index.setdefault(target, set()).update([key, *lowered])
    return {k: tuple(sorted(v - {k})) for k, v in index.items()}


# Component types that are leaves of a larger feature. When two results match
# the same number of query terms, the implementation unit (class, LWC, flow,
# object) is more useful than one of its fields — the issue asked for exactly
# this: "フィールドよりコンポーネントを優先する".
_LEAF_TYPES = frozenset(
    {
        "CustomField",
        "CustomLabel",
        "ValidationRule",
        "ListView",
        "CompactLayout",
        "WebLink",
        "FieldSet",
        "RecordType",
    }
)


def _type_rank(component_type: str) -> int:
    """1 for an implementation unit, 0 for a leaf of one."""
    return 0 if component_type in _LEAF_TYPES else 1


# Boolean operators are syntax, not search terms — they must not be counted
# as "matched" when a verbatim FTS5 query is ranked.
_FTS_OPERATORS = {"and", "or", "not", "near"}


class _Term(NamedTuple):
    """One query term: the forms the user typed, and what they also mean."""

    direct: tuple[str, ...]
    synonyms: tuple[str, ...]

    @property
    def variants(self) -> tuple[str, ...]:
        return self.direct + self.synonyms


def _plan_terms(query: str, synonyms: dict[str, tuple[str, ...]]) -> list[_Term]:
    """Build the term plan used for retrieval, ranking and snippets."""
    terms: list[_Term] = []
    for variants in _ranking_groups(query):
        expanded: list[str] = []
        for variant in variants:
            for synonym in synonyms.get(variant, ()):
                if synonym not in variants and synonym not in expanded:
                    expanded.append(synonym)
        terms.append(_Term(tuple(variants), tuple(expanded)))
    return terms


def _score_terms(
    name: str, raw_text: str, terms: list[_Term]
) -> tuple[int, int, int]:
    """
    Score a candidate against the query plan.

    Returns (matched, name_hits, direct_hits):
      matched      — terms found anywhere in the component
      name_hits    — terms found in the component's own name, which is a far
                     stronger signal than a mention in the body text
      direct_hits  — terms found in the form the user typed, as opposed to
                     only through a synonym
    """
    name_text = f"{name} {_camel_split_text(name)}".lower()
    body = f"{name_text}\n{raw_text}".lower()
    name_norm = _normalize(name_text)
    body_norm = _normalize(body)

    matched = name_hits = direct_hits = 0
    for term in terms:
        in_name = any(v in name_text or v in name_norm for v in term.variants)
        in_body = in_name or any(v in body or v in body_norm for v in term.variants)
        if not in_body:
            continue
        matched += 1
        if in_name:
            name_hits += 1
        if any(v in body or v in body_norm for v in term.direct):
            direct_hits += 1
    return matched, name_hits, direct_hits


def _camel_split_text(text: str) -> str:
    """Split identifiers so "SelfRegistrationController" also reads as words."""
    spaced = re.sub(r"(?<=[a-z0-9])([A-Z])", r" \1", text)
    spaced = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", spaced)
    return re.sub(r"[_\-.]", " ", spaced)


def _ranking_groups(query: str) -> list[list[str]]:
    """
    Term groups used for ranking and snippets.

    Same as the retrieval terms, minus FTS5 syntax: a prefix query like
    `Staff*` ranks on "staff", and the operators in `a OR b` are skipped.
    """
    groups: list[list[str]] = []
    for term in _query_terms(query):
        cleaned = term.strip('*"').strip()
        if not cleaned or cleaned.lower() in _FTS_OPERATORS:
            continue
        groups.append(_term_variants(cleaned))
    return groups


# ---------------------------------------------------------------------------
# Database schema
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS components (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    type        TEXT NOT NULL,
    file_path   TEXT NOT NULL,
    yaml_spec   TEXT NOT NULL,
    raw_text    TEXT NOT NULL,
    updated_at  REAL NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS components_fts USING fts5(
    name,
    type,
    raw_text,
    content='components',
    content_rowid='id',
    tokenize="trigram"
);

CREATE TABLE IF NOT EXISTS embeddings (
    component_id INTEGER PRIMARY KEY REFERENCES components(id),
    vocab_json   TEXT NOT NULL,
    vector_json  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS annotations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    component_id INTEGER NOT NULL REFERENCES components(id) ON DELETE CASCADE,
    key          TEXT NOT NULL,
    value        TEXT NOT NULL,
    source       TEXT NOT NULL DEFAULT 'manual',
    created_at   REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_annotations_component ON annotations(component_id);

-- Triggers to keep FTS in sync
CREATE TRIGGER IF NOT EXISTS components_ai AFTER INSERT ON components BEGIN
    INSERT INTO components_fts(rowid, name, type, raw_text)
    VALUES (new.id, new.name, new.type, new.raw_text);
END;

CREATE TRIGGER IF NOT EXISTS components_au AFTER UPDATE ON components BEGIN
    UPDATE components_fts SET
        name     = new.name,
        type     = new.type,
        raw_text = new.raw_text
    WHERE rowid = new.id;
END;

CREATE TRIGGER IF NOT EXISTS components_ad AFTER DELETE ON components BEGIN
    DELETE FROM components_fts WHERE rowid = old.id;
END;
"""


# ---------------------------------------------------------------------------
# SearchEngine
# ---------------------------------------------------------------------------


class SearchEngine:
    """
    Hybrid search engine for indexed Salesforce metadata.

    Uses SQLite FTS5 for keyword search. If numpy is installed, also provides
    bag-of-words cosine similarity for re-ranking results.
    """

    def __init__(self, project_root: str | Path = ".") -> None:
        self.project_root = Path(project_root).resolve()
        self.rtk_dir = self.project_root / RTK_DIR
        self.db_path = self.rtk_dir / DB_FILE
        self.specs_dir = self.rtk_dir / SPECS_DIR
        self._conn: sqlite3.Connection | None = None
        self._synonyms: dict[str, tuple[str, ...]] | None = None

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self.rtk_dir.mkdir(exist_ok=True)
            self._conn = sqlite3.connect(str(self.db_path))
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(_SCHEMA_SQL)
            self._conn.commit()
        return self._conn

    def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "SearchEngine":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Vocabulary
    # ------------------------------------------------------------------

    def synonyms(self) -> dict[str, tuple[str, ...]]:
        """
        The JA↔EN term table: built-ins plus `.rtk-sf/synonyms.yaml`.

        The project file wins, so a team can map its own wording onto its own
        component names. A malformed file is ignored rather than breaking
        search — it is an optional relevance aid, not an index.
        """
        if self._synonyms is not None:
            return self._synonyms

        table = dict(_BUILTIN_SYNONYMS)
        path = self.rtk_dir / SYNONYMS_FILE
        if path.exists():
            try:
                import yaml  # lazy import

                loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                for key, value in loaded.items():
                    targets = value if isinstance(value, (list, tuple)) else [value]
                    table[str(key)] = tuple(str(t) for t in targets if str(t).strip())
            except Exception as exc:
                logger.warning("Could not read %s: %s", path, exc)

        self._synonyms = _build_synonym_index(table)
        return self._synonyms

    # ------------------------------------------------------------------
    # Ingestion
    # ------------------------------------------------------------------

    def upsert_component(
        self,
        name: str,
        component_type: str,
        file_path: str,
        yaml_spec: str,
        raw_text: str,
        updated_at: float,
    ) -> None:
        """
        Insert or update a component in the search index.

        Args:
            name: Component name (e.g. "AccountService").
            component_type: Type string (e.g. "ApexClass", "CustomObject").
            file_path: Absolute path to the source file.
            yaml_spec: Compressed YAML specification string.
            raw_text: Full source text for FTS indexing.
            updated_at: File modification timestamp (mtime).
        """
        conn = self._get_conn()
        existing = conn.execute(
            "SELECT id FROM components WHERE name = ?", (name,)
        ).fetchone()

        if existing:
            conn.execute(
                """UPDATE components SET type=?, file_path=?, yaml_spec=?, raw_text=?, updated_at=?
                   WHERE name=?""",
                (component_type, file_path, yaml_spec, raw_text, updated_at, name),
            )
        else:
            conn.execute(
                """INSERT INTO components (name, type, file_path, yaml_spec, raw_text, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (name, component_type, file_path, yaml_spec, raw_text, updated_at),
            )

        conn.commit()

        # Build and store bag-of-words embedding if numpy is available
        if _NUMPY_AVAILABLE:
            self._upsert_embedding(name, raw_text, conn)

    def _upsert_embedding(
        self, name: str, raw_text: str, conn: sqlite3.Connection
    ) -> None:
        """Compute and store a bag-of-words vector for the component."""
        # Build vocab from this component's text (top 200 tokens by length)
        tokens = list(set(raw_text.lower().split()))
        tokens.sort(key=len, reverse=True)
        vocab = tokens[:200]
        vector = _bag_of_words_vector(raw_text, vocab)

        row = conn.execute(
            "SELECT component_id FROM embeddings WHERE component_id = "
            "(SELECT id FROM components WHERE name = ?)",
            (name,),
        ).fetchone()

        if row:
            conn.execute(
                "UPDATE embeddings SET vocab_json=?, vector_json=? WHERE component_id="
                "(SELECT id FROM components WHERE name=?)",
                (json.dumps(vocab), json.dumps(vector), name),
            )
        else:
            conn.execute(
                "INSERT INTO embeddings (component_id, vocab_json, vector_json) "
                "VALUES ((SELECT id FROM components WHERE name=?), ?, ?)",
                (name, json.dumps(vocab), json.dumps(vector)),
            )
        conn.commit()

    def delete_component(self, name: str) -> None:
        """Remove a component from the search index."""
        conn = self._get_conn()
        conn.execute("DELETE FROM components WHERE name = ?", (name,))
        conn.commit()

    # ------------------------------------------------------------------
    # Annotations — reverse-record discovered business logic
    # ------------------------------------------------------------------

    def add_annotation(
        self,
        component_name: str,
        key: str,
        value: str,
        source: str = "manual",
    ) -> bool:
        """
        Record a discovered business rule or context note on a component.

        The annotation text is appended to the component's FTS raw_text so
        it becomes searchable immediately without a full re-index.

        Args:
            component_name: Exact component name (e.g. "Entry__c").
            key: Short label for the annotation (e.g. "business_rule", "condition").
            value: Free-text description of the discovered logic.
            source: Origin tag (e.g. "ai_discovery", "manual", "code_review").

        Returns:
            True if the annotation was saved; False if component not found.
        """
        import time

        conn = self._get_conn()
        row = conn.execute(
            "SELECT id FROM components WHERE name = ?", (component_name,)
        ).fetchone()
        if not row:
            return False

        component_id = row["id"]
        conn.execute(
            "INSERT INTO annotations (component_id, key, value, source, created_at) VALUES (?, ?, ?, ?, ?)",
            (component_id, key, value, source, time.time()),
        )

        # Append annotation text to raw_text so FTS index reflects it
        combined = f"\n\n[annotation:{key}] {value}"
        conn.execute(
            "UPDATE components SET raw_text = raw_text || ? WHERE id = ?",
            (combined.lower(), component_id),
        )
        conn.execute(
            "UPDATE components_fts SET raw_text = raw_text || ? WHERE rowid = ?",
            (combined.lower(), component_id),
        )

        conn.commit()
        return True

    def get_annotations(self, component_name: str) -> list[dict[str, Any]]:
        """
        Return all annotations for a component.

        Args:
            component_name: Exact component name.

        Returns:
            List of dicts with keys: key, value, source, created_at.
        """
        conn = self._get_conn()
        rows = conn.execute(
            """
            SELECT a.key, a.value, a.source, a.created_at
            FROM annotations a
            JOIN components c ON c.id = a.component_id
            WHERE c.name = ?
            ORDER BY a.created_at ASC
            """,
            (component_name,),
        ).fetchall()
        return [
            {
                "key": r["key"],
                "value": r["value"],
                "source": r["source"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        """
        Search indexed components for *query*.

        Terms are matched independently and results are ranked by how many of
        them a component contains, so a multi-word query (in any language)
        degrades to "best overlap" instead of returning nothing when no single
        component holds every term.

        Retrieval widens only as far as it has to:

          1. FTS5 with all trigram-searchable terms AND-ed — the exact answer.
          2. FTS5 with those terms OR-ed — partial matches, ranked by overlap.
          3. LIKE scan requiring every term, then any term — reaches terms the
             trigram index cannot represent (1–2 characters, e.g. "職員").

        A query containing explicit FTS5 syntax (quotes, `*`, parentheses,
        AND/OR/NOT/NEAR) is passed to FTS5 verbatim.

        Args:
            query: Search query string.
            limit: Maximum number of results to return.

        Returns:
            List of result dicts with keys: name, type, file_path, snippet,
            score, matched_terms, yaml_spec.
        """
        conn = self._get_conn()
        query = query.strip()
        if not query:
            return []

        terms = _plan_terms(query, self.synonyms())
        groups = [list(t.variants) for t in terms]
        # Over-fetch: ranking by term overlap needs more than `limit` rows to
        # choose from, especially on the OR and LIKE paths.
        pool = max(limit * 6, 30)

        rows: list[sqlite3.Row] = []

        if _FTS_SYNTAX_RE.search(query):
            rows = self._fts_rows(conn, query, pool)

        if not rows and groups:
            searchable = [g for g in groups if _is_trigram_searchable(g)]
            if searchable:
                exprs = [_fts_group(g) for g in searchable]
                rows = self._fts_rows(conn, " AND ".join(exprs), pool)
                if not rows and len(exprs) > 1:
                    rows = self._fts_rows(conn, " OR ".join(exprs), pool)

        if not rows and groups:
            rows = self._like_rows(conn, groups, pool, require_all=True)
            if not rows and len(groups) > 1:
                rows = self._like_rows(conn, groups, pool, require_all=False)

        results: list[dict[str, Any]] = []
        for row in rows:
            raw_text = row["raw_text"]
            matched, name_hits, direct_hits = _score_terms(
                row["name"], raw_text, terms
            )
            results.append(
                {
                    "name": row["name"],
                    "type": row["type"],
                    "file_path": row["file_path"],
                    "snippet": self._make_snippet(raw_text, query, terms=terms),
                    "score": float(row["fts_score"] or 0),
                    "matched_terms": matched,
                    "name_matches": name_hits,
                    "direct_matches": direct_hits,
                    "yaml_spec": row["yaml_spec"],
                }
            )

        if _NUMPY_AVAILABLE and results:
            self._attach_cosine(query, results, conn)

        # Ranking, most significant first:
        #   1. how many query terms the component matched at all
        #   2. matches on the component's own name — `SelfRegistrationController`
        #      is what "セルフ登録" is asking for, not a field that mentions it
        #   3. implementation units over their leaves (class/LWC over one field)
        #   4. terms matched as typed, over terms reached via a synonym
        #   5. cosine similarity, then FTS rank (lower is better; 0 on LIKE)
        results.sort(
            key=lambda r: (
                -r["matched_terms"],
                -r["name_matches"],
                -_type_rank(r["type"]),
                -r["direct_matches"],
                -r.get("cosine_score", 0.0),
                r["score"],
            )
        )
        return results[:limit]

    # ------------------------------------------------------------------
    # Retrieval paths
    # ------------------------------------------------------------------

    _SELECT_FTS = """
        SELECT c.name, c.type, c.file_path, c.yaml_spec, c.raw_text,
               fts.rank AS fts_score
        FROM components_fts fts
        JOIN components c ON c.id = fts.rowid
        WHERE components_fts MATCH ?
        ORDER BY fts.rank
        LIMIT ?
    """

    @staticmethod
    def _fts_rows(
        conn: sqlite3.Connection, expression: str, limit: int
    ) -> list[sqlite3.Row]:
        """Run an FTS5 MATCH, returning [] on a syntax error instead of raising."""
        try:
            return conn.execute(
                SearchEngine._SELECT_FTS, (expression, limit)
            ).fetchall()
        except sqlite3.OperationalError as exc:
            logger.debug("FTS5 rejected %r: %s", expression, exc)
            return []

    @staticmethod
    def _like_rows(
        conn: sqlite3.Connection,
        groups: list[list[str]],
        limit: int,
        require_all: bool,
    ) -> list[sqlite3.Row]:
        """
        Substring scan over name + raw_text.

        This is the only path that can match a 1–2 character term, which the
        trigram index cannot represent at all.
        """
        clauses: list[str] = []
        params: list[Any] = []
        for variants in groups:
            per_variant = []
            for variant in variants:
                per_variant.append("(name LIKE ? OR raw_text LIKE ?)")
                params += [f"%{variant}%", f"%{variant}%"]
            clauses.append("(" + " OR ".join(per_variant) + ")")

        joiner = " AND " if require_all else " OR "
        sql = (
            "SELECT name, type, file_path, yaml_spec, raw_text, 0 AS fts_score "
            "FROM components WHERE " + joiner.join(clauses) + " LIMIT ?"
        )
        params.append(limit)
        return conn.execute(sql, params).fetchall()

    @staticmethod
    def _attach_cosine(
        query: str, results: list[dict[str, Any]], conn: sqlite3.Connection
    ) -> None:
        """
        Annotate each result with its bag-of-words cosine score.

        Used only as a tiebreaker behind term overlap: on its own it ranked a
        component that merely shares vocabulary above one that matched every
        query term.
        """
        names = [r["name"] for r in results]
        placeholders = ",".join("?" * len(names))
        emb_rows = conn.execute(
            f"SELECT c.name, e.vocab_json, e.vector_json "
            f"FROM embeddings e JOIN components c ON c.id = e.component_id "
            f"WHERE c.name IN ({placeholders})",
            names,
        ).fetchall()
        emb_map = {r["name"]: r for r in emb_rows}

        for result in results:
            emb_row = emb_map.get(result["name"])
            if not emb_row:
                result["cosine_score"] = 0.0
                continue
            vocab = json.loads(emb_row["vocab_json"])
            doc_vec = json.loads(emb_row["vector_json"])
            query_vec = _bag_of_words_vector(query, vocab)
            result["cosine_score"] = _cosine_similarity(query_vec, doc_vec)

    @staticmethod
    def _make_snippet(
        text: str,
        query: str,
        window: int = 150,
        terms: list[_Term] | None = None,
    ) -> str:
        """
        Extract a text snippet around the first matching query term.

        Every term is tried, not just the first one: with an OR match the
        leading term is often the one that is absent, which used to pin the
        snippet to the top of the spec and hide why the result was returned.
        Terms the user typed are preferred over their synonyms, so the snippet
        shows the words that were actually searched for when they are present.
        """
        lower_text = text.lower()
        if terms is None:
            terms = [_Term(tuple(v), ()) for v in _ranking_groups(query)]

        pos = -1
        for forms in ([t.direct for t in terms], [t.synonyms for t in terms]):
            for variants in forms:
                for variant in variants:
                    pos = lower_text.find(variant)
                    if pos != -1:
                        break
                if pos != -1:
                    break
            if pos != -1:
                break
        if pos == -1:
            return text[:window].strip()
        start = max(0, pos - window // 2)
        end = min(len(text), pos + window // 2)
        snippet = text[start:end].strip()
        if start > 0:
            snippet = "..." + snippet
        if end < len(text):
            snippet = snippet + "..."
        return snippet

    # ------------------------------------------------------------------
    # Direct spec access
    # ------------------------------------------------------------------

    def get_spec(self, component_name: str) -> str | None:
        """
        Retrieve the compressed YAML spec for a component by name.

        Args:
            component_name: Exact component name (case-sensitive).

        Returns:
            YAML string, or None if not found.
        """
        # Try DB first
        conn = self._get_conn()
        row = conn.execute(
            "SELECT yaml_spec FROM components WHERE name = ?", (component_name,)
        ).fetchone()
        if row:
            return row["yaml_spec"]

        # Fall back to spec file on disk
        spec_path = self.specs_dir / f"{component_name}.yaml"
        if spec_path.exists():
            return spec_path.read_text(encoding="utf-8")

        return None

    def list_components(self, component_type: str = "all") -> list[dict[str, str]]:
        """
        List all indexed components.

        Args:
            component_type: Filter by type ("ApexClass", "CustomObject",
                            "CustomField", "Flow") or "all".

        Returns:
            List of dicts with keys: name, type, file_path.
        """
        conn = self._get_conn()
        if component_type == "all":
            rows = conn.execute(
                "SELECT name, type, file_path FROM components ORDER BY type, name"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT name, type, file_path FROM components WHERE type = ? ORDER BY name",
                (component_type,),
            ).fetchall()

        return [{"name": r["name"], "type": r["type"], "file_path": r["file_path"]} for r in rows]

    def get_relations(self, component_name: str) -> dict[str, Any]:
        """
        Get upstream (callers) and downstream (callees) relations for a component.

        Reads from `.rtk-sf/relations.json` built by the indexer.

        Args:
            component_name: Component name to look up.

        Returns:
            dict with keys: component, upstream (list), downstream (list).
        """
        relations_path = self.rtk_dir / RELATIONS_FILE
        if not relations_path.exists():
            return {"component": component_name, "upstream": [], "downstream": []}

        with open(relations_path) as f:
            relations = json.load(f)

        edges = relations.get("edges", [])
        upstream = [e["source"] for e in edges if e["target"] == component_name]
        downstream = [e["target"] for e in edges if e["source"] == component_name]

        return {
            "component": component_name,
            "upstream": upstream,
            "downstream": downstream,
        }

    @staticmethod
    def _camel_split(text: str) -> str:
        """
        Insert spaces before uppercase letters to split camelCase/PascalCase
        identifiers into searchable words.

        Example: "ReportDownloadController" → "Report Download Controller"
        """
        import re
        # Insert space before uppercase letter followed by lowercase
        spaced = re.sub(r"(?<=[a-z0-9])([A-Z])", r" \1", text)
        # Insert space between consecutive uppercase and then lowercase (e.g. XMLParser)
        spaced = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", spaced)
        # Also split on underscores and hyphens
        spaced = re.sub(r"[_\-]", " ", spaced)
        return spaced

    def _build_raw_text(self, name: str, yaml_text: str) -> str:
        """
        Build an FTS-friendly raw text blob from YAML spec + split identifiers.

        Appends camelCase-split versions of all identifiers so that FTS5 can
        match individual word fragments (e.g. 'exam' matches
        'ReportDownloadController').
        """
        import re
        # Extract all identifiers from the YAML
        identifiers = re.findall(r"[A-Za-z][A-Za-z0-9_]{3,}", yaml_text)
        unique = list(dict.fromkeys(identifiers))
        split_words = " ".join(self._camel_split(ident) for ident in unique[:200])
        return f"{yaml_text}\n\n{split_words}".lower()

    def sync_from_specs(self) -> int:
        """
        Populate the SQLite database from YAML spec files on disk.

        Useful after running the indexer to make specs searchable.

        Returns:
            Number of components synced.
        """
        if not self.specs_dir.exists():
            logger.warning("Specs directory not found: %s", self.specs_dir)
            return 0

        import yaml  # lazy import

        count = 0
        for spec_file in self.specs_dir.glob("*.yaml"):
            try:
                yaml_text = spec_file.read_text(encoding="utf-8")
                data = yaml.safe_load(yaml_text)
                name = data.get("component", spec_file.stem)
                component_type = data.get("type", "Unknown")
                file_path = data.get("file", "")

                raw_text = self._build_raw_text(name, yaml_text)
                self.upsert_component(
                    name=name,
                    component_type=component_type,
                    file_path=file_path,
                    yaml_spec=yaml_text,
                    raw_text=raw_text,
                    updated_at=spec_file.stat().st_mtime,
                )
                count += 1
            except Exception as exc:
                logger.warning("Could not sync spec %s: %s", spec_file, exc)

        logger.info("Synced %d components from specs directory.", count)
        return count
