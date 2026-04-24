"""Per-project long-term memory: SQLite + FTS5 BM25 with optional dense rerank.

The store lives at `.iceq/memory.sqlite` in the user's working directory.
Each iteration (user/assistant/tool result) becomes one row.

Phase 1 — lexical only (FTS5 BM25). Always available, zero deps.
Phase 2 — semantic boost (this file): every ingested row is also embedded
via Ollama's /api/embed endpoint (default model: nomic-embed-text). Search
becomes a two-stage hybrid pipeline:
    1. BM25 returns ~4*top_k candidates from FTS5
    2. Embed the query once, cosine-rerank the candidate set
    3. Final score = alpha*bm25_norm + (1-alpha)*cosine_norm

Embeddings are pre-normalized to unit length on insert so cosine reduces to
a dot product at query time. Vectors are stored as compact float32 BLOBs
via stdlib `array` — no numpy dependency.

If the embedding endpoint is unreachable or the model is not pulled, the
embedding client marks itself unavailable on first failure and the system
silently falls back to BM25-only. No errors are propagated to the caller.
"""

import array
import json as _json
import os
import re
import sqlite3
import time

import requests

from localscript import config

_DB_DIR = ".iceq"
_DB_NAME = "memory.sqlite"

# Characters that have meaning in FTS5 query syntax — strip them when
# building a query from raw user text so we never trip a syntax error.
_FTS_STRIP_RE = re.compile(r'[\"\(\)\*\:\^\-\+]')

# Schema version stored in the `meta` table.
#   v1 = phase 1 (FTS5 BM25 only)
#   v2 = phase 2 (adds embedding BLOB column)
#   v3 = adds content_indexed column so JSON tool-call noise can be stripped
#        from FTS5 indexing without losing the raw content for display
_SCHEMA_VERSION = 3

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    turn_idx INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    content_indexed TEXT,
    ts INTEGER NOT NULL,
    embedding BLOB
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(ts);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content_indexed,
    content='messages',
    content_rowid='id',
    tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content_indexed)
    VALUES (new.id, new.content_indexed);
END;

CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content_indexed)
    VALUES('delete', old.id, old.content_indexed);
END;

-- Only fire on content_indexed changes so backfilling the embedding
-- column doesn't redundantly reindex FTS5.
CREATE TRIGGER IF NOT EXISTS messages_au
    AFTER UPDATE OF content_indexed ON messages
BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content_indexed)
    VALUES('delete', old.id, old.content_indexed);
    INSERT INTO messages_fts(rowid, content_indexed)
    VALUES (new.id, new.content_indexed);
END;
"""

# Fields we keep when stripping a JSON tool call before FTS5 indexing.
# These carry the lexical signal a search would actually want to hit:
# tool name, file paths, queries, summaries, message text, package names.
_TOOLCALL_SIGNAL_KEYS = ("path", "file", "query", "summary", "text", "package")


def _strip_index_noise(content: str) -> str:
    """Reduce a JSON tool call to its high-signal fields for FTS5 indexing.

    Long write_file/patch_file blobs full of generated code (variable names,
    Lua keywords, comments) hijack BM25 ranking — every search for "csv parser"
    matches every code file that happens to mention csv or parser. This
    helper extracts just the tool name + file paths + queries + summaries,
    leaving everything else out of the lexical index.

    Non-JSON content (user messages, tool results, complete_task summaries
    that are plain text) is returned unchanged. If the JSON has no signal
    fields at all, the original content is returned so the row stays
    searchable on whatever tokens it does carry.
    """
    if not content:
        return content
    stripped = content.strip()
    if not stripped.startswith("{"):
        return content
    try:
        obj = _json.loads(stripped)
    except (_json.JSONDecodeError, ValueError):
        return content
    if not isinstance(obj, dict):
        return content
    parts: list[str] = []
    tool = obj.get("tool") or obj.get("method")
    if isinstance(tool, str) and tool.strip():
        parts.append(tool.strip())
    for key in _TOOLCALL_SIGNAL_KEYS:
        v = obj.get(key)
        if isinstance(v, str) and v.strip():
            parts.append(v.strip())
    return " ".join(parts) if parts else content


def _build_fts_query(text: str) -> str:
    """Turn raw user text into a safe FTS5 OR query."""
    cleaned = _FTS_STRIP_RE.sub(" ", text or "")
    seen: set[str] = set()
    terms: list[str] = []
    for tok in cleaned.split():
        tok = tok.strip()
        if len(tok) < 2:
            continue
        low = tok.lower()
        if low in seen:
            continue
        seen.add(low)
        terms.append(f'"{tok}"')
    return " OR ".join(terms)


# ---------------------------------------------------------------------------
# Vector helpers — pure Python, no numpy dependency
# ---------------------------------------------------------------------------

def _pack_vec(vec: list[float]) -> bytes:
    """Pack a float vector into a compact float32 BLOB for SQLite storage."""
    return array.array("f", vec).tobytes()


def _unpack_vec(blob: bytes | None) -> list[float] | None:
    if not blob:
        return None
    arr = array.array("f")
    arr.frombytes(blob)
    return list(arr)


def _normalize(vec: list[float]) -> list[float]:
    """Return a unit-length copy of *vec*. Zero-vectors are returned as-is."""
    norm_sq = 0.0
    for x in vec:
        norm_sq += x * x
    if norm_sq <= 0.0:
        return list(vec)
    inv = 1.0 / (norm_sq ** 0.5)
    return [x * inv for x in vec]


def _dot(a: list[float], b: list[float]) -> float:
    """Dot product. For unit vectors this equals cosine similarity."""
    s = 0.0
    for x, y in zip(a, b):
        s += x * y
    return s


# ---------------------------------------------------------------------------
# Embedding client — talks to Ollama /api/embed
# ---------------------------------------------------------------------------

class _EmbeddingClient:
    """Thin wrapper around Ollama's /api/embed endpoint.

    Tracks reachability: after the first failed call (network down, 404,
    model not pulled, etc.) we set `available=False` so we never retry
    until the user starts a new Context. This avoids hangs in tight loops.
    """

    def __init__(self, url: str, model: str, timeout: float = 10.0):
        self.url = url
        self.model = model
        self.timeout = timeout
        self.available: bool = True
        self.last_error: str | None = None

    def embed(self, text: str) -> list[float] | None:
        out = self.embed_batch([text])
        return out[0] if out else None

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed a list of texts in one request. Returns [] on any failure."""
        if not self.available or not texts:
            return []
        try:
            resp = requests.post(
                self.url,
                json={"model": self.model, "input": texts},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            embeddings = data.get("embeddings") or []
            if not embeddings or len(embeddings) != len(texts):
                self.available = False
                self.last_error = (
                    f"unexpected response shape: got {len(embeddings)} embeddings "
                    f"for {len(texts)} inputs"
                )
                return []
            # Pre-normalize so cosine == dot at query time.
            return [_normalize(e) for e in embeddings]
        except Exception as e:
            self.available = False
            self.last_error = str(e)
            return []


# ---------------------------------------------------------------------------
# Memory store
# ---------------------------------------------------------------------------

class Memory:
    """Per-project memory store. One instance per Context.

    Lazy: nothing touches disk until the first add() or search().
    """

    def __init__(
        self,
        root: str | None = None,
        embedding_client: _EmbeddingClient | None = None,
    ):
        self._root = root or os.getcwd()
        self._db_path = os.path.join(self._root, _DB_DIR, _DB_NAME)
        self._conn: sqlite3.Connection | None = None

        # Embedding client. If the caller passed one (tests), use it.
        # Otherwise build the default Ollama client when phase 2 is enabled.
        if embedding_client is not None:
            self._embedder: _EmbeddingClient | None = embedding_client
        elif config.MEMORY_EMBEDDINGS:
            self._embedder = _EmbeddingClient(
                config.MEMORY_EMBEDDING_URL,
                config.MEMORY_EMBEDDING_MODEL,
            )
        else:
            self._embedder = None

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(_SCHEMA_SQL)
        # Mark fresh DBs as already at the latest version so _migrate sees
        # current=_SCHEMA_VERSION and skips. Existing DBs already have the
        # meta row at their older version; INSERT OR IGNORE no-ops on them
        # and _migrate runs the necessary deltas.
        conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
            (str(_SCHEMA_VERSION),),
        )
        self._migrate(conn)
        # Update the version row after migration succeeds.
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
            (str(_SCHEMA_VERSION),),
        )
        conn.commit()
        self._conn = conn
        return conn

    def _migrate(self, conn: sqlite3.Connection):
        """Apply in-place migrations for older schemas."""
        row = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
        current = int(row[0]) if row else 0
        if current >= _SCHEMA_VERSION:
            return

        # v1 -> v2: add embedding column on existing messages table.
        if current < 2:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(messages)").fetchall()]
            if "embedding" not in cols:
                conn.execute("ALTER TABLE messages ADD COLUMN embedding BLOB")

        # v2 -> v3: separate displayed content from FTS-indexed content so
        # JSON tool-call bodies can be stripped without losing the raw text.
        # Migration order matters here: we must drop the old FTS table and
        # ALL triggers BEFORE the backfill UPDATE, because the executescript
        # above may have installed a new messages_au trigger (if none existed
        # in the v2 DB) that references content_indexed — which would try to
        # write into the old FTS table that still has the wrong column. So:
        #   1. drop all triggers (silence any pending FTS sync)
        #   2. drop the old FTS5 virtual table
        #   3. add the content_indexed column on messages
        #   4. backfill it with cleaned content (no triggers fire)
        #   5. recreate the new FTS5 table + triggers against content_indexed
        #   6. rebuild the FTS index from messages.content_indexed in one shot
        if current < 3:
            conn.execute("DROP TRIGGER IF EXISTS messages_ai")
            conn.execute("DROP TRIGGER IF EXISTS messages_ad")
            conn.execute("DROP TRIGGER IF EXISTS messages_au")
            conn.execute("DROP TABLE IF EXISTS messages_fts")
            cols = [r[1] for r in conn.execute("PRAGMA table_info(messages)").fetchall()]
            if "content_indexed" not in cols:
                conn.execute("ALTER TABLE messages ADD COLUMN content_indexed TEXT")
            pending = conn.execute(
                "SELECT id, content FROM messages WHERE content_indexed IS NULL"
            ).fetchall()
            for row_id, content in pending:
                conn.execute(
                    "UPDATE messages SET content_indexed = ? WHERE id = ?",
                    (_strip_index_noise(content), row_id),
                )
            conn.executescript("""
                CREATE VIRTUAL TABLE messages_fts USING fts5(
                    content_indexed,
                    content='messages',
                    content_rowid='id',
                    tokenize='porter unicode61'
                );
                CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN
                    INSERT INTO messages_fts(rowid, content_indexed)
                    VALUES (new.id, new.content_indexed);
                END;
                CREATE TRIGGER messages_ad AFTER DELETE ON messages BEGIN
                    INSERT INTO messages_fts(messages_fts, rowid, content_indexed)
                    VALUES('delete', old.id, old.content_indexed);
                END;
                CREATE TRIGGER messages_au
                    AFTER UPDATE OF content_indexed ON messages
                BEGIN
                    INSERT INTO messages_fts(messages_fts, rowid, content_indexed)
                    VALUES('delete', old.id, old.content_indexed);
                    INSERT INTO messages_fts(rowid, content_indexed)
                    VALUES (new.id, new.content_indexed);
                END;
                INSERT INTO messages_fts(messages_fts) VALUES('rebuild');
            """)
            conn.commit()

    def close(self):
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    @property
    def db_path(self) -> str:
        return self._db_path

    def exists(self) -> bool:
        return os.path.isfile(self._db_path)

    @property
    def embeddings_available(self) -> bool:
        """True if an embedding client exists AND it has not failed yet."""
        return self._embedder is not None and self._embedder.available

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    def add(self, session_id: str, turn_idx: int, role: str, content: str) -> int:
        """Append one message. Embeds it if phase 2 is active. Returns row id.

        The full *content* is stored verbatim for display, while a stripped
        copy is written to *content_indexed* for FTS5 to index.
        """
        if not content or not content.strip():
            return -1
        conn = self._connect()
        embedding_blob: bytes | None = None
        if self.embeddings_available:
            # Embed the full content (not the stripped version) so the dense
            # channel still benefits from the semantic richness of code bodies.
            vec = self._embedder.embed(content)  # type: ignore[union-attr]
            if vec:
                embedding_blob = _pack_vec(vec)
        indexed = _strip_index_noise(content)
        cur = conn.execute(
            "INSERT INTO messages"
            "(session_id, turn_idx, role, content, content_indexed, ts, embedding) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (session_id, turn_idx, role, content, indexed, int(time.time()), embedding_blob),
        )
        conn.commit()
        return cur.lastrowid or -1

    def add_many(self, session_id: str, items: list[tuple[int, str, str]]):
        """Bulk insert (turn_idx, role, content) tuples in one transaction.

        Embeds the whole batch in a single /api/embed call when phase 2 is on.
        Each row also gets its `content_indexed` computed from the raw content.
        """
        if not items:
            return
        # Drop empty content first so embedding indices line up with rows.
        filtered = [
            (idx, role, content)
            for idx, role, content in items
            if content and content.strip()
        ]
        if not filtered:
            return

        embeddings: list[bytes | None] = [None] * len(filtered)
        if self.embeddings_available:
            texts = [c for _, _, c in filtered]
            vecs = self._embedder.embed_batch(texts)  # type: ignore[union-attr]
            if vecs and len(vecs) == len(filtered):
                embeddings = [_pack_vec(v) for v in vecs]

        conn = self._connect()
        now = int(time.time())
        conn.executemany(
            "INSERT INTO messages"
            "(session_id, turn_idx, role, content, content_indexed, ts, embedding) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (session_id, idx, role, content, _strip_index_noise(content), now, emb)
                for (idx, role, content), emb in zip(filtered, embeddings)
            ],
        )
        conn.commit()

    # ------------------------------------------------------------------
    # Backfill — embed existing rows that have NULL embedding
    # ------------------------------------------------------------------

    def backfill_embeddings(self, batch_size: int = 32) -> dict:
        """Embed every row whose embedding is NULL.

        Stops cleanly if the embedding API fails mid-run, leaving partial
        progress in place. Returns {backfilled, remaining, skipped}.
        """
        if not self.exists():
            return {"backfilled": 0, "remaining": 0, "skipped": True}
        if not self.embeddings_available:
            return {"backfilled": 0, "remaining": 0, "skipped": True}
        conn = self._connect()
        pending = conn.execute(
            "SELECT id, content FROM messages WHERE embedding IS NULL ORDER BY id"
        ).fetchall()
        if not pending:
            return {"backfilled": 0, "remaining": 0, "skipped": False}

        backfilled = 0
        for i in range(0, len(pending), batch_size):
            chunk = pending[i:i + batch_size]
            texts = [row[1] for row in chunk]
            vecs = self._embedder.embed_batch(texts)  # type: ignore[union-attr]
            if not vecs or len(vecs) != len(chunk):
                # API failed mid-backfill. Stop and report partial progress.
                break
            for (row_id, _), vec in zip(chunk, vecs):
                conn.execute(
                    "UPDATE messages SET embedding=? WHERE id=?",
                    (_pack_vec(vec), row_id),
                )
                backfilled += 1
            conn.commit()

        remaining = len(pending) - backfilled
        return {"backfilled": backfilled, "remaining": remaining, "skipped": False}

    # ------------------------------------------------------------------
    # Read path — two-stage hybrid retrieval
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        top_k: int = 8,
        exclude_ids: set[int] | None = None,
    ) -> list[dict]:
        """BM25 + (optional) cosine rerank.

        Stage 1: pull `4 * top_k` BM25 candidates from FTS5.
        Stage 2: if embeddings are live, embed the query once, score each
                 candidate by cosine vs its stored vector, then combine
                 with BM25 via `MEMORY_HYBRID_ALPHA`.

        Falls back gracefully to BM25-only if the embedder is unavailable
        or any candidate has a NULL embedding (that row uses its BM25
        score as the dense score for ranking purposes).
        """
        fts_query = _build_fts_query(query)
        if not fts_query or not self.exists():
            return []

        use_dense = self.embeddings_available
        candidate_count = top_k * 4 if use_dense else top_k
        limit = candidate_count + (len(exclude_ids) if exclude_ids else 0)

        conn = self._connect()
        try:
            cur = conn.execute(
                """
                SELECT m.id, m.session_id, m.turn_idx, m.role, m.content, m.ts,
                       m.embedding, bm25(messages_fts) AS rank
                FROM messages_fts
                JOIN messages m ON m.id = messages_fts.rowid
                WHERE messages_fts MATCH ?
                ORDER BY rank ASC
                LIMIT ?
                """,
                (fts_query, limit),
            )
        except sqlite3.OperationalError:
            return []

        candidates: list[dict] = []
        for row in cur:
            row_id = row[0]
            if exclude_ids and row_id in exclude_ids:
                continue
            candidates.append({
                "id": row_id,
                "session_id": row[1],
                "turn_idx": row[2],
                "role": row[3],
                "content": row[4],
                "ts": row[5],
                "_embedding": _unpack_vec(row[6]),
                "_bm25_raw": float(row[7]),  # negative; lower = better
            })
            if len(candidates) >= candidate_count:
                break

        if not candidates:
            return []

        # Embed the query exactly once. If it fails, drop dense reranking
        # for this call but the client stays "available" only if the failure
        # was a clean None — _EmbeddingClient.embed() flips available on errors.
        query_vec: list[float] | None = None
        if use_dense:
            query_vec = self._embedder.embed(query)  # type: ignore[union-attr]
            if not query_vec:
                use_dense = False

        if use_dense and query_vec:
            # Min-max normalize BM25 over the candidate set so it lives
            # in [0, 1] alongside the cosine score.
            bm_pos = [-c["_bm25_raw"] for c in candidates]  # higher = better
            bm_min = min(bm_pos)
            bm_max = max(bm_pos)
            bm_range = (bm_max - bm_min) or 1.0
            alpha = float(config.MEMORY_HYBRID_ALPHA)
            for c, raw in zip(candidates, bm_pos):
                bm_norm = (raw - bm_min) / bm_range
                emb = c["_embedding"]
                if emb:
                    cos = _dot(query_vec, emb)
                    cos_norm = (cos + 1.0) / 2.0  # [-1, 1] -> [0, 1]
                else:
                    # No embedding for this row — let BM25 carry it.
                    cos_norm = bm_norm
                c["score"] = round(alpha * bm_norm + (1.0 - alpha) * cos_norm, 4)
            candidates.sort(key=lambda c: c["score"], reverse=True)
        else:
            for c in candidates:
                c["score"] = round(-c["_bm25_raw"], 3)
            # Already ordered by ASC bm25 (lower=better) which means higher
            # positive scores are first — but be explicit for clarity.
            candidates.sort(key=lambda c: c["score"], reverse=True)

        # Strip internal fields and trim to top_k.
        return [
            {
                "id": c["id"],
                "session_id": c["session_id"],
                "turn_idx": c["turn_idx"],
                "role": c["role"],
                "content": c["content"],
                "ts": c["ts"],
                "score": c["score"],
            }
            for c in candidates[:top_k]
        ]

    def recent_ids(self, session_id: str, n: int) -> list[int]:
        """Return the row ids of the last *n* messages of a session."""
        if not self.exists() or n <= 0:
            return []
        conn = self._connect()
        cur = conn.execute(
            "SELECT id FROM messages WHERE session_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (session_id, n),
        )
        return [r[0] for r in cur]

    def delete_from(self, session_id: str, min_id: int) -> int:
        """Delete every row in *session_id* with id >= *min_id*.

        Used by /undo to drop a whole turn's worth of memory rows. The
        FTS5 ad trigger fires automatically and removes the corresponding
        index entries. Returns the number of rows deleted.
        """
        if not self.exists() or min_id <= 0:
            return 0
        conn = self._connect()
        cur = conn.execute(
            "DELETE FROM messages WHERE session_id = ? AND id >= ?",
            (session_id, min_id),
        )
        conn.commit()
        return cur.rowcount or 0

    # ------------------------------------------------------------------
    # Stats and admin
    # ------------------------------------------------------------------

    def stats(self) -> dict:
        """Return store stats including embedding coverage."""
        if not self.exists():
            return {
                "rows": 0,
                "sessions": 0,
                "embedded": 0,
                "db_bytes": 0,
                "exists": False,
                "embeddings_available": self.embeddings_available,
                "embedding_model": (
                    self._embedder.model if self._embedder else None
                ),
            }
        conn = self._connect()
        rows = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        sessions = conn.execute(
            "SELECT COUNT(DISTINCT session_id) FROM messages"
        ).fetchone()[0]
        embedded = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE embedding IS NOT NULL"
        ).fetchone()[0]
        try:
            db_bytes = os.path.getsize(self._db_path)
        except OSError:
            db_bytes = 0
        return {
            "rows": rows,
            "sessions": sessions,
            "embedded": embedded,
            "db_bytes": db_bytes,
            "exists": True,
            "embeddings_available": self.embeddings_available,
            "embedding_model": self._embedder.model if self._embedder else None,
        }

    def clear(self):
        """Wipe all rows. Schema and file remain."""
        if not self.exists():
            return
        conn = self._connect()
        conn.execute("DELETE FROM messages")
        conn.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
        conn.commit()
