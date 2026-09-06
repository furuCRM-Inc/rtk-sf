"""
search.py — SQLite FTS5 + optional vector hybrid search engine.

Provides keyword search over indexed Salesforce metadata specs using SQLite's
built-in FTS5 full-text search. When numpy is available, cosine similarity
is layered on top for semantic ranking.

Usage:
    engine = SearchEngine("/path/to/project")
    results = engine.search("AccountService", limit=5)
    spec = engine.get_spec("AccountService")
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

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
    """
    words = set(text.lower().split())
    return [1.0 if term in words else 0.0 for term in vocab]


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
    content_rowid='id'
);

CREATE TABLE IF NOT EXISTS embeddings (
    component_id INTEGER PRIMARY KEY REFERENCES components(id),
    vocab_json   TEXT NOT NULL,
    vector_json  TEXT NOT NULL
);

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
    # Search
    # ------------------------------------------------------------------

    def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        """
        Search indexed components using FTS5 keyword search.

        If numpy is available, results are re-ranked using cosine similarity
        against a bag-of-words representation of the query.

        Args:
            query: Search query string.
            limit: Maximum number of results to return.

        Returns:
            List of result dicts with keys: name, type, file_path, snippet, score.
        """
        conn = self._get_conn()

        # Sanitize query for FTS5 (escape double-quotes)
        safe_query = query.replace('"', '""')

        try:
            rows = conn.execute(
                """
                SELECT c.name, c.type, c.file_path, c.yaml_spec, c.raw_text,
                       fts.rank AS fts_score
                FROM components_fts fts
                JOIN components c ON c.id = fts.rowid
                WHERE components_fts MATCH ?
                ORDER BY fts.rank
                LIMIT ?
                """,
                (safe_query, limit * 3),  # over-fetch for re-ranking
            ).fetchall()
        except sqlite3.OperationalError:
            # FTS5 query syntax error — fall back to LIKE search
            like_q = f"%{query}%"
            rows = conn.execute(
                """
                SELECT name, type, file_path, yaml_spec, raw_text, 0 AS fts_score
                FROM components
                WHERE name LIKE ? OR raw_text LIKE ?
                LIMIT ?
                """,
                (like_q, like_q, limit * 3),
            ).fetchall()

        results: list[dict[str, Any]] = []
        for row in rows:
            snippet = self._make_snippet(row["raw_text"], query)
            score = float(row["fts_score"] or 0)
            results.append(
                {
                    "name": row["name"],
                    "type": row["type"],
                    "file_path": row["file_path"],
                    "snippet": snippet,
                    "score": score,
                    "yaml_spec": row["yaml_spec"],
                }
            )

        # Re-rank with cosine similarity when numpy is available
        if _NUMPY_AVAILABLE and results:
            results = self._rerank_with_cosine(query, results, conn)

        return results[:limit]

    def _rerank_with_cosine(
        self,
        query: str,
        results: list[dict[str, Any]],
        conn: sqlite3.Connection,
    ) -> list[dict[str, Any]]:
        """Re-rank FTS results using cosine similarity."""
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
            if emb_row:
                vocab = json.loads(emb_row["vocab_json"])
                doc_vec = json.loads(emb_row["vector_json"])
                query_vec = _bag_of_words_vector(query, vocab)
                result["cosine_score"] = _cosine_similarity(query_vec, doc_vec)
            else:
                result["cosine_score"] = 0.0

        # Combine FTS rank and cosine score (lower FTS rank = better)
        results.sort(key=lambda r: r["cosine_score"], reverse=True)
        return results

    @staticmethod
    def _make_snippet(text: str, query: str, window: int = 150) -> str:
        """Extract a text snippet around the first query term occurrence."""
        lower_text = text.lower()
        lower_query = query.lower().split()[0] if query.split() else ""
        pos = lower_text.find(lower_query)
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

                # Use YAML as both spec and raw text for FTS
                self.upsert_component(
                    name=name,
                    component_type=component_type,
                    file_path=file_path,
                    yaml_spec=yaml_text,
                    raw_text=yaml_text,
                    updated_at=spec_file.stat().st_mtime,
                )
                count += 1
            except Exception as exc:
                logger.warning("Could not sync spec %s: %s", spec_file, exc)

        logger.info("Synced %d components from specs directory.", count)
        return count
