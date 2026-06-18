"""SQLite-based embedding and metadata cache.

Replaces the pickle-based cache with atomic per-row operations.
Stores both embeddings and message metadata for fast query without
re-reading source files.
"""

import base64
import hashlib
import logging
import sqlite3
import time
from pathlib import Path

import numpy as np

from .config import get_config
from .migrations import run_migrations

logger = logging.getLogger(__name__)


def _get_cache_db_path(fingerprint: str) -> Path:
    """Get the cache database path, namespaced by backend fingerprint."""
    config = get_config()
    cache_path = config.embedding.cache_path
    cache_path.mkdir(parents=True, exist_ok=True)
    fp_hash = hashlib.md5(fingerprint.encode()).hexdigest()[:12]
    return cache_path / f"cache_{fp_hash}.db"


class EmbeddingCache:
    """SQLite-backed embedding cache with metadata storage.

    Tables:
    - embeddings: text_hash -> embedding vector (blob)
    - messages: metadata about indexed messages (for query without re-reading files)
    - session_state: file-level mtime tracking for incremental indexing
    - execution_index: chatSessionId -> exec file path mapping
    """

    def __init__(self, fingerprint: str):
        self._fingerprint = fingerprint
        self._db_path = _get_cache_db_path(fingerprint)
        self._conn: sqlite3.Connection | None = None

    @property
    def db_path(self) -> Path:
        return self._db_path

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA cache_size=-64000")  # 64MB page cache
            self._create_tables()
        return self._conn

    def _create_tables(self):
        """Create all tables if they don't exist."""
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS embeddings (
                text_hash TEXT PRIMARY KEY,
                embedding BLOB NOT NULL,
                dimensions INTEGER NOT NULL,
                created_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS messages (
                uuid TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                workspace TEXT NOT NULL,
                timestamp REAL NOT NULL,
                role TEXT NOT NULL,
                searchable_text TEXT NOT NULL,
                message_index INTEGER NOT NULL,
                source TEXT NOT NULL,
                text_hash TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_messages_session
                ON messages(session_id);
            CREATE INDEX IF NOT EXISTS idx_messages_workspace
                ON messages(workspace);
            CREATE INDEX IF NOT EXISTS idx_messages_timestamp
                ON messages(timestamp);
            CREATE INDEX IF NOT EXISTS idx_messages_text_hash
                ON messages(text_hash);

            CREATE TABLE IF NOT EXISTS session_state (
                session_id TEXT PRIMARY KEY,
                mtime REAL NOT NULL,
                fingerprint TEXT NOT NULL,
                indexed_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS execution_index (
                exec_file_path TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                file_mtime REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_exec_session
                ON execution_index(session_id);

            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)
        # Store fingerprint as metadata
        self.conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('fingerprint', ?)",
            (self._fingerprint,),
        )
        self.conn.commit()

        # Run any pending schema migrations
        run_migrations(self.conn)

    def close(self):
        """Close the database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None

    # --- Embedding operations ---

    def get_embedding(self, text_hash: str) -> np.ndarray | None:
        """Get a cached embedding by text hash."""
        row = self.conn.execute(
            "SELECT embedding, dimensions FROM embeddings WHERE text_hash = ?",
            (text_hash,),
        ).fetchone()
        if row is None:
            return None
        blob, dims = row
        return np.frombuffer(blob, dtype=np.float32).reshape(dims)

    def has_embedding(self, text_hash: str) -> bool:
        """Check if an embedding exists in cache."""
        row = self.conn.execute(
            "SELECT 1 FROM embeddings WHERE text_hash = ? LIMIT 1",
            (text_hash,),
        ).fetchone()
        return row is not None

    def put_embedding(self, text_hash: str, embedding: np.ndarray):
        """Store an embedding in the cache."""
        blob = embedding.astype(np.float32).tobytes()
        self.conn.execute(
            "INSERT OR REPLACE INTO embeddings (text_hash, embedding, dimensions, created_at) VALUES (?, ?, ?, ?)",
            (text_hash, blob, embedding.shape[0], time.time()),
        )
        self.conn.commit()

    def put_embeddings_batch(self, items: list[tuple[str, np.ndarray]]):
        """Store multiple embeddings in a single transaction."""
        now = time.time()
        rows = [
            (text_hash, emb.astype(np.float32).tobytes(), emb.shape[0], now)
            for text_hash, emb in items
        ]
        self.conn.executemany(
            "INSERT OR REPLACE INTO embeddings (text_hash, embedding, dimensions, created_at) VALUES (?, ?, ?, ?)",
            rows,
        )
        self.conn.commit()

    def get_all_embedding_hashes(self) -> set[str]:
        """Get all text hashes that have cached embeddings."""
        cursor = self.conn.execute("SELECT text_hash FROM embeddings")
        return {row[0] for row in cursor}

    def get_embeddings_batch(self, text_hashes: list[str]) -> dict[str, np.ndarray]:
        """Get multiple embeddings by text hash."""
        if not text_hashes:
            return {}
        # SQLite has a limit on placeholders, batch if needed
        result = {}
        batch_size = 500
        for i in range(0, len(text_hashes), batch_size):
            batch = text_hashes[i:i + batch_size]
            placeholders = ",".join("?" * len(batch))
            cursor = self.conn.execute(
                f"SELECT text_hash, embedding, dimensions FROM embeddings WHERE text_hash IN ({placeholders})",
                batch,
            )
            for row in cursor:
                text_hash, blob, dims = row
                result[text_hash] = np.frombuffer(blob, dtype=np.float32).reshape(dims)
        return result

    @property
    def embedding_count(self) -> int:
        """Get total number of cached embeddings."""
        return self.conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]

    def put_message(self, uuid: str, session_id: str, workspace: str,
                    timestamp: float, role: str, searchable_text: str,
                    message_index: int, source: str, text_hash: str,
                    content_tier: str = "conversation", tool_name: str | None = None):
        """Store message metadata."""
        self.conn.execute(
            """INSERT OR REPLACE INTO messages
               (uuid, session_id, workspace, timestamp, role, searchable_text,
                message_index, source, text_hash, content_tier, tool_name)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (uuid, session_id, workspace, timestamp, role, searchable_text,
             message_index, source, text_hash, content_tier, tool_name),
        )

    def put_messages_batch(self, messages: list[tuple]):
        """Store multiple message metadata in a single transaction.

        Each tuple: (uuid, session_id, workspace, timestamp, role, searchable_text,
                     message_index, source, text_hash, content_tier, tool_name)

        For backward compatibility, tuples with 9 elements (without content_tier
        and tool_name) are also accepted — they default to 'conversation' tier
        with no tool_name.
        """
        # Normalize tuples to 11 elements for the INSERT
        normalized = []
        for msg in messages:
            if len(msg) == 9:
                normalized.append(msg + ("conversation", None))
            else:
                normalized.append(msg)

        self.conn.executemany(
            """INSERT OR REPLACE INTO messages
               (uuid, session_id, workspace, timestamp, role, searchable_text,
                message_index, source, text_hash, content_tier, tool_name)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            normalized,
        )
        self.conn.commit()

    def get_session_messages(self, session_id: str) -> list[dict]:
        """Get all messages for a session, sorted by message_index."""
        cursor = self.conn.execute(
            """SELECT uuid, session_id, workspace, timestamp, role, searchable_text,
                      message_index, source, text_hash, content_tier, tool_name
               FROM messages WHERE session_id = ? ORDER BY message_index""",
            (session_id,),
        )
        cols = [
            "uuid", "session_id", "workspace", "timestamp", "role",
            "searchable_text", "message_index", "source", "text_hash",
            "content_tier", "tool_name",
        ]
        return [dict(zip(cols, row)) for row in cursor]

    def delete_session_messages(self, session_id: str):
        """Delete all messages for a session (for re-indexing)."""
        self.conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))

    @property
    def message_count(self) -> int:
        """Get total number of indexed messages."""
        return self.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]

    # --- Full-text search operations ---

    def fts_search(
        self,
        query: str,
        workspace: str | None = None,
        source: str | None = None,
        after_ts: float | None = None,
        before_ts: float | None = None,
        limit: int = 100,
        include_tool_context: bool = False,
    ) -> list[dict]:
        """Full-text search using FTS5 with BM25 ranking.

        Returns results sorted by BM25 relevance score (negative values,
        more negative = more relevant). Applies workspace/source/date filters.

        When include_tool_context is False (default), only matches against
        content_tier='conversation' messages. When True, also matches against
        'tool_context' messages.

        Returns list of dicts with keys matching the messages table columns
        plus a 'fts_score' key (normalized to 0-1 range, higher = better).
        """
        # Check if FTS table exists (may not if migration hasn't run yet)
        try:
            self.conn.execute("SELECT 1 FROM messages_fts LIMIT 0")
        except sqlite3.OperationalError:
            return []

        # Build the WHERE clause for filters
        conditions = []
        params: list = []

        if workspace:
            # Bidirectional: match sessions that are parents OR children of the query workspace
            # Also match base64-encoded legacy values
            encoded_ws = base64.urlsafe_b64encode(workspace.encode()).decode().rstrip("=")
            conditions.append(
                "(m.workspace LIKE ? || '%' OR ? LIKE m.workspace || '%' "
                "OR m.workspace LIKE ? || '%')"
            )
            params.append(workspace)
            params.append(workspace)
            params.append(encoded_ws)
        if source:
            conditions.append("m.source = ?")
            params.append(source)
        if after_ts:
            conditions.append("m.timestamp >= ?")
            params.append(after_ts)
        if before_ts:
            conditions.append("m.timestamp < ?")
            params.append(before_ts)

        # Content tier filtering: default to conversation only
        if not include_tool_context:
            conditions.append("m.content_tier = ?")
            params.append("conversation")

        where_clause = ""
        if conditions:
            where_clause = "AND " + " AND ".join(conditions)

        # FTS5 MATCH query — quote each token to prevent interpretation as column names
        # or operators. This handles terms like "ruby" that FTS5 would otherwise
        # try to interpret as column references.
        fts_query = " ".join(
            f'"{token.replace(chr(34), chr(34)+chr(34))}"'
            for token in query.split()
            if token.strip()
        )

        sql = f"""
            SELECT m.uuid, m.session_id, m.workspace, m.timestamp, m.role,
                   m.searchable_text, m.message_index, m.source,
                   rank AS fts_rank, m.content_tier, m.tool_name
            FROM messages_fts
            JOIN messages m ON messages_fts.rowid = m.rowid
            WHERE messages_fts MATCH ?
            {where_clause}
            ORDER BY rank
            LIMIT ?
        """
        params = [fts_query] + params + [limit]

        try:
            cursor = self.conn.execute(sql, params)
        except (sqlite3.OperationalError, sqlite3.DatabaseError):
            # FTS query syntax error (e.g., special chars) — fall back to no results
            return []

        rows = cursor.fetchall()
        if not rows:
            return []

        # Normalize BM25 scores to 0-1 range (rank is negative, more negative = better)
        # Take the best (most negative) score as the reference
        raw_scores = [row[8] for row in rows]
        best_score = min(raw_scores)  # Most negative
        worst_score = max(raw_scores)  # Least negative (or 0)
        score_range = worst_score - best_score if worst_score != best_score else 1.0

        results = []
        for row in rows:
            uuid, session_id, ws, ts, role, text, msg_idx, src, fts_rank, content_tier, tool_name = row
            # Normalize: best match → 1.0, worst match → ~0.1
            normalized_score = 1.0 - ((fts_rank - best_score) / score_range) * 0.9
            result = {
                "uuid": uuid,
                "session_id": session_id,
                "workspace": ws,
                "timestamp": ts,
                "role": role,
                "content": text[:2000] + "..." if len(text) > 2000 else text,
                "message_index": msg_idx,
                "source": src,
                "score": normalized_score,
                "content_tier": content_tier,
            }
            if tool_name is not None:
                result["tool_name"] = tool_name
            results.append(result)

        return results

    # --- Session state operations ---

    def get_session_mtime(self, session_id: str) -> float | None:
        """Get stored mtime for a session, or None if not tracked."""
        row = self.conn.execute(
            "SELECT mtime FROM session_state WHERE session_id = ? AND fingerprint = ?",
            (session_id, self._fingerprint),
        ).fetchone()
        return row[0] if row else None

    def update_session_state(self, session_id: str, mtime: float):
        """Update session state after successful indexing."""
        self.conn.execute(
            "INSERT OR REPLACE INTO session_state (session_id, mtime, fingerprint, indexed_at) VALUES (?, ?, ?, ?)",
            (session_id, mtime, self._fingerprint, time.time()),
        )
        self.conn.commit()

    def clear_session_state(self):
        """Clear all session state (for force rebuild)."""
        self.conn.execute("DELETE FROM session_state")
        self.conn.commit()

    @property
    def indexed_session_count(self) -> int:
        """Count sessions indexed with current fingerprint."""
        return self.conn.execute(
            "SELECT COUNT(*) FROM session_state WHERE fingerprint = ?",
            (self._fingerprint,),
        ).fetchone()[0]

    # --- Execution index operations ---

    def get_exec_files_for_session(self, session_id: str) -> list[str]:
        """Get execution log file paths for a session."""
        cursor = self.conn.execute(
            "SELECT exec_file_path FROM execution_index WHERE session_id = ?",
            (session_id,),
        )
        return [row[0] for row in cursor]

    def put_exec_file(self, exec_file_path: str, session_id: str, file_mtime: float):
        """Store an execution file -> session mapping."""
        self.conn.execute(
            "INSERT OR REPLACE INTO execution_index (exec_file_path, session_id, file_mtime) VALUES (?, ?, ?)",
            (exec_file_path, session_id, file_mtime),
        )

    def put_exec_files_batch(self, items: list[tuple[str, str, float]]):
        """Store multiple execution file mappings.

        Each tuple: (exec_file_path, session_id, file_mtime)
        """
        self.conn.executemany(
            "INSERT OR REPLACE INTO execution_index (exec_file_path, session_id, file_mtime) VALUES (?, ?, ?)",
            items,
        )
        self.conn.commit()

    def get_exec_index_count(self) -> int:
        """Get total execution files indexed."""
        return self.conn.execute("SELECT COUNT(*) FROM execution_index").fetchone()[0]

    def get_all_exec_file_mtimes(self) -> dict[str, float]:
        """Get all stored exec file paths and their mtimes."""
        cursor = self.conn.execute("SELECT exec_file_path, file_mtime FROM execution_index")
        return {row[0]: row[1] for row in cursor}

    # --- Metadata operations ---

    def get_meta(self, key: str) -> str | None:
        """Get a metadata value by key."""
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str):
        """Set a metadata value."""
        self.conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            (key, value),
        )
        self.conn.commit()
