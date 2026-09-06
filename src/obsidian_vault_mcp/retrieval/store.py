"""Sqlite-vec-backed chunk store.

Per RETRIEVAL-DESIGN.md §2.2: one row per chunk holds the raw chunk text,
heading breadcrumb, file path, line anchor, frontmatter-derived filter
fields, and the embedding vector, all together -- no separate vector-database
service. See PORT-DESIGN.md for why sqlite-vec was chosen over Postgres.

Per §2.3: content-hash diffing at the chunk level, per file, so re-indexing
is proportional to what actually changed. Per §3.1: folder-scoping is a
`WHERE` clause on the retrieval query itself, applied INSIDE the same query
that does the vector KNN match -- verified empirically (not assumed) against
this sqlite-vec version: a `chunk_id IN (subquery)` predicate combined with
`embedding MATCH ... AND k = ...` genuinely restricts the KNN candidate set,
it does not filter an already-computed unrestricted top-k afterward.
"""

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import sqlite_vec

from .chunker import Chunk


@dataclass(frozen=True)
class QueryResult:
    file_path: str
    text: str
    heading_path: tuple[str, ...]
    line_start: int
    parent_id: str
    content_hash: str
    frontmatter: dict
    distance: float


def _scope_predicate(prefix: str) -> tuple[str, tuple]:
    """SQL predicate + params for 'path is under this folder prefix'.

    Mirrors the exact boundary logic tools/search.py already uses for
    frontmatter-index scoping (`p == scope or p.startswith(scope + "/")`):
    a prefix of "General" matches "General" and "General/...", never
    "GeneralOther/...". Empty prefix = unrestricted (root scope).
    """
    if not prefix:
        return "1=1", ()
    return "(file_path = ? OR file_path LIKE ?)", (prefix, prefix + "/%")


class RetrievalStore:
    def __init__(self, db_path: str | Path, embedding_dim: int):
        self.embedding_dim = embedding_dim
        self._conn = sqlite3.connect(str(db_path))
        self._conn.enable_load_extension(True)
        sqlite_vec.load(self._conn)
        self._conn.enable_load_extension(False)
        self._init_schema()

    def close(self) -> None:
        self._conn.close()

    def _init_schema(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chunks (
                id INTEGER PRIMARY KEY,
                file_path TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                parent_id TEXT NOT NULL,
                text TEXT NOT NULL,
                heading_path TEXT NOT NULL,
                line_start INTEGER NOT NULL,
                frontmatter TEXT NOT NULL,
                UNIQUE(file_path, content_hash)
            )
            """
        )
        self._conn.execute(
            f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS chunk_vectors USING vec0(
                chunk_id INTEGER PRIMARY KEY,
                embedding FLOAT[{self.embedding_dim}]
            )
            """
        )
        self._conn.commit()

    # -- incremental re-indexing (§2.3) -----------------------------------

    def existing_hashes(self, file_path: str) -> set[str]:
        rows = self._conn.execute(
            "SELECT content_hash FROM chunks WHERE file_path = ?", (file_path,)
        ).fetchall()
        return {r[0] for r in rows}

    def diff(self, file_path: str, chunks: list[Chunk]) -> tuple[list[Chunk], list[Chunk]]:
        """Split `chunks` (the freshly re-chunked current content of a file)
        into (needs_embedding, already_stored) by comparing content hashes
        against what's already stored for this exact file path.
        """
        existing = self.existing_hashes(file_path)
        to_embed = [c for c in chunks if c.content_hash not in existing]
        reused = [c for c in chunks if c.content_hash in existing]
        return to_embed, reused

    def apply(
        self,
        file_path: str,
        chunks: list[Chunk],
        new_embeddings: dict[str, list[float]],
    ) -> None:
        """Reconcile stored rows for `file_path` to exactly match `chunks`.

        - Rows whose hash is no longer in `chunks` are deleted (content was
          edited away, merged, or the section removed).
        - Rows whose hash already exists keep their stored embedding
          untouched (no re-embedding call), but their metadata (line anchor,
          parent id, frontmatter) is refreshed to match the current chunk --
          cheap, no API call, and keeps citations accurate.
        - Rows whose hash is new are inserted using the caller-supplied
          embedding from `new_embeddings`.
        """
        current_hashes = {c.content_hash for c in chunks}
        existing = self.existing_hashes(file_path)

        stale = existing - current_hashes
        if stale:
            ids = self._conn.execute(
                "SELECT id FROM chunks WHERE file_path = ? AND content_hash IN (%s)"
                % ",".join("?" * len(stale)),
                (file_path, *stale),
            ).fetchall()
            for (row_id,) in ids:
                self._conn.execute("DELETE FROM chunk_vectors WHERE chunk_id = ?", (row_id,))
            self._conn.execute(
                "DELETE FROM chunks WHERE file_path = ? AND content_hash IN (%s)"
                % ",".join("?" * len(stale)),
                (file_path, *stale),
            )

        # Two chunks CAN legitimately end up with the same content_hash within
        # one call -- e.g. a real repeated log line chunked twice from the
        # same file (found live against Jett's vault: Memory/Claude
        # Interaction Notes.md, Daily/telegram-conversations-*.md). Their
        # embedded text is byte-identical, so collapsing to one stored row
        # loses no retrievable information; only a per-chunk detail like
        # line_start differs, and keeping the first occurrence is an
        # arbitrary but harmless tie-break. Without this, the second one
        # would violate the (file_path, content_hash) unique constraint.
        seen_this_call: set[str] = set()

        for c in chunks:
            if c.content_hash in seen_this_call:
                continue
            seen_this_call.add(c.content_hash)

            if c.content_hash in existing:
                self._conn.execute(
                    """UPDATE chunks SET parent_id = ?, text = ?, heading_path = ?,
                       line_start = ?, frontmatter = ? WHERE file_path = ? AND content_hash = ?""",
                    (
                        c.parent_id,
                        c.text,
                        json.dumps(list(c.heading_path)),
                        c.line_start,
                        # default=str: YAML frontmatter can hold non-JSON-native
                        # types (an unquoted `date: 2026-06-05` parses to a real
                        # datetime.date, not a string) -- found live against
                        # Jett's vault. Never let an exotic frontmatter value
                        # crash indexing; stringify what json can't represent
                        # natively rather than raising.
                        json.dumps(c.frontmatter, default=str),
                        file_path,
                        c.content_hash,
                    ),
                )
            else:
                vec = new_embeddings.get(c.content_hash)
                if vec is None:
                    raise ValueError(
                        f"no embedding supplied for new chunk hash {c.content_hash!r} in {file_path!r}"
                    )
                cur = self._conn.execute(
                    """INSERT INTO chunks (file_path, content_hash, parent_id, text, heading_path, line_start, frontmatter)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        file_path,
                        c.content_hash,
                        c.parent_id,
                        c.text,
                        json.dumps(list(c.heading_path)),
                        c.line_start,
                        json.dumps(c.frontmatter, default=str),
                    ),
                )
                row_id = cur.lastrowid
                self._conn.execute(
                    "INSERT INTO chunk_vectors (chunk_id, embedding) VALUES (?, ?)",
                    (row_id, sqlite_vec.serialize_float32(vec)),
                )

        self._conn.commit()

    def delete_file(self, file_path: str) -> None:
        ids = self._conn.execute("SELECT id FROM chunks WHERE file_path = ?", (file_path,)).fetchall()
        for (row_id,) in ids:
            self._conn.execute("DELETE FROM chunk_vectors WHERE chunk_id = ?", (row_id,))
        self._conn.execute("DELETE FROM chunks WHERE file_path = ?", (file_path,))
        self._conn.commit()

    # -- query path (§3) ---------------------------------------------------

    def query(
        self,
        query_vector: list[float],
        path_prefix: str = "",
        top_k: int = 10,
        max_distance: float | None = None,
        must_contain: str | None = None,
        must_not_contain: str | None = None,
        frontmatter_field: str | None = None,
        frontmatter_value: str | None = None,
    ) -> list[QueryResult]:
        """Vector-similarity search, scoped and filtered BEFORE ranking.

        `path_prefix` is applied as part of the same subquery the KNN match
        is restricted to -- a chunk outside it is never a candidate the
        vector search considers, not a result filtered out afterward.
        """
        where_sql, where_params = _scope_predicate(path_prefix)
        params: list = list(where_params)

        if must_contain:
            where_sql += " AND text LIKE ?"
            params.append(f"%{must_contain}%")
        if must_not_contain:
            where_sql += " AND text NOT LIKE ?"
            params.append(f"%{must_not_contain}%")
        if frontmatter_field is not None:
            where_sql += " AND json_extract(frontmatter, ?) = ?"
            params.append(f"$.{frontmatter_field}")
            params.append(frontmatter_value)

        sql = f"""
            SELECT c.file_path, c.text, c.heading_path, c.line_start, c.parent_id,
                   c.content_hash, c.frontmatter, v.distance
            FROM chunk_vectors v
            JOIN chunks c ON c.id = v.chunk_id
            WHERE v.embedding MATCH ? AND k = ?
              AND v.chunk_id IN (SELECT id FROM chunks WHERE {where_sql})
            ORDER BY v.distance
        """
        rows = self._conn.execute(
            sql, (sqlite_vec.serialize_float32(query_vector), top_k, *params)
        ).fetchall()

        results = []
        for file_path, text, heading_path_json, line_start, parent_id, content_hash, fm_json, distance in rows:
            if max_distance is not None and distance > max_distance:
                continue
            results.append(
                QueryResult(
                    file_path=file_path,
                    text=text,
                    heading_path=tuple(json.loads(heading_path_json)),
                    line_start=line_start,
                    parent_id=parent_id,
                    content_hash=content_hash,
                    frontmatter=json.loads(fm_json),
                    distance=distance,
                )
            )
        return results
