"""SQLite persistence (local/dev/tests). Same surface as SupabaseDB."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DB:
    """Thread-safe SQLite persistence for sessions, messages, tool runs, usage, docs."""

    backend = "sqlite"

    def __init__(self, path: Path):
        self.path = str(path)
        self._lock = threading.Lock()
        self._init()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._conn() as c:
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions(
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL DEFAULT '00000000-0000-0000-0000-000000000001',
                    created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS messages(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL, user_id TEXT NOT NULL DEFAULT '00000000-0000-0000-0000-000000000001',
                    role TEXT NOT NULL, content TEXT NOT NULL, meta TEXT,
                    created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS tool_runs(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL, user_id TEXT NOT NULL DEFAULT '00000000-0000-0000-0000-000000000001',
                    tool TEXT NOT NULL, args TEXT, result TEXT, ok INTEGER,
                    latency_ms REAL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS usage(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL, user_id TEXT NOT NULL DEFAULT '00000000-0000-0000-0000-000000000001',
                    prompt_tokens INTEGER, completion_tokens INTEGER,
                    created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS session_docs(
                    session_id TEXT NOT NULL, doc_id TEXT NOT NULL,
                    user_id TEXT NOT NULL DEFAULT '00000000-0000-0000-0000-000000000001',
                    PRIMARY KEY (session_id, doc_id));
                CREATE TABLE IF NOT EXISTS documents(
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL DEFAULT '00000000-0000-0000-0000-000000000001',
                    name TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    storage_path TEXT NOT NULL,
                    fingerprint TEXT,
                    n_rows INTEGER NOT NULL DEFAULT 0,
                    n_cols INTEGER NOT NULL DEFAULT 0,
                    schema_card TEXT,
                    audit TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(user_id, name));
                """
            )
            # Lightweight migrations for older DBs missing user_id.
            for table, col in (
                ("sessions", "user_id"),
                ("messages", "user_id"),
                ("tool_runs", "user_id"),
                ("usage", "user_id"),
                ("session_docs", "user_id"),
            ):
                cols = {r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}
                if col not in cols:
                    c.execute(
                        f"ALTER TABLE {table} ADD COLUMN {col} TEXT NOT NULL "
                        "DEFAULT '00000000-0000-0000-0000-000000000001'"
                    )

    # sessions -------------------------------------------------------------
    def create_session(
        self, user_id: str = "00000000-0000-0000-0000-000000000001"
    ) -> str:
        sid = uuid.uuid4().hex[:12]
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO sessions(id, user_id, created_at) VALUES(?, ?, ?)",
                (sid, user_id, utcnow()),
            )
        return sid

    def session_exists(self, sid: str, user_id: str | None = None) -> bool:
        with self._lock, self._conn() as c:
            if user_id:
                row = c.execute(
                    "SELECT 1 FROM sessions WHERE id=? AND user_id=?", (sid, user_id)
                ).fetchone()
            else:
                row = c.execute("SELECT 1 FROM sessions WHERE id=?", (sid,)).fetchone()
        return row is not None

    def list_sessions(
        self, user_id: str = "00000000-0000-0000-0000-000000000001", limit: int = 40
    ) -> list[dict]:
        lim = max(1, min(int(limit), 100))
        with self._lock, self._conn() as c:
            rows = c.execute(
                """
                SELECT s.id, s.created_at,
                       (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id) AS n_messages,
                       (SELECT content FROM messages m
                          WHERE m.session_id = s.id AND m.role = 'user'
                          ORDER BY m.id LIMIT 1) AS preview,
                       (SELECT MAX(created_at) FROM messages m
                          WHERE m.session_id = s.id) AS last_activity
                FROM sessions s
                WHERE s.user_id = ?
                ORDER BY COALESCE(last_activity, s.created_at) DESC
                LIMIT ?
                """,
                (user_id, lim),
            ).fetchall()
        return [
            {
                "session_id": r["id"],
                "created_at": r["created_at"],
                "last_activity": r["last_activity"] or r["created_at"],
                "n_messages": int(r["n_messages"] or 0),
                "preview": (r["preview"] or "New session")[:120],
            }
            for r in rows
        ]

    def delete_session(self, sid: str, user_id: str | None = None) -> None:
        with self._lock, self._conn() as c:
            if user_id:
                row = c.execute(
                    "SELECT 1 FROM sessions WHERE id=? AND user_id=?", (sid, user_id)
                ).fetchone()
                if not row:
                    return
            for table in ("messages", "tool_runs", "usage", "session_docs"):
                c.execute(f"DELETE FROM {table} WHERE session_id=?", (sid,))
            c.execute("DELETE FROM sessions WHERE id=?", (sid,))

    # session documents ----------------------------------------------------
    def set_session_docs(
        self,
        sid: str,
        doc_ids: list[str],
        user_id: str = "00000000-0000-0000-0000-000000000001",
    ) -> None:
        with self._lock, self._conn() as c:
            c.execute("DELETE FROM session_docs WHERE session_id=?", (sid,))
            for did in doc_ids:
                c.execute(
                    "INSERT OR IGNORE INTO session_docs(session_id, doc_id, user_id)"
                    " VALUES(?, ?, ?)",
                    (sid, did, user_id),
                )

    def attach_session_doc(
        self,
        sid: str,
        doc_id: str,
        user_id: str = "00000000-0000-0000-0000-000000000001",
    ) -> None:
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO session_docs(session_id, doc_id, user_id)"
                " VALUES(?, ?, ?)",
                (sid, doc_id, user_id),
            )

    def detach_session_doc(self, sid: str, doc_id: str, user_id: str | None = None) -> None:
        with self._lock, self._conn() as c:
            c.execute(
                "DELETE FROM session_docs WHERE session_id=? AND doc_id=?",
                (sid, doc_id),
            )

    def get_session_docs(self, sid: str, user_id: str | None = None) -> list[str]:
        with self._lock, self._conn() as c:
            rows = c.execute(
                "SELECT doc_id FROM session_docs WHERE session_id=? ORDER BY doc_id",
                (sid,),
            ).fetchall()
        return [r["doc_id"] for r in rows]

    def detach_doc_everywhere(self, doc_id: str, user_id: str | None = None) -> None:
        with self._lock, self._conn() as c:
            if user_id:
                c.execute(
                    "DELETE FROM session_docs WHERE doc_id=? AND user_id=?",
                    (doc_id, user_id),
                )
            else:
                c.execute("DELETE FROM session_docs WHERE doc_id=?", (doc_id,))

    # documents ------------------------------------------------------------
    def upsert_document(self, row: dict) -> None:
        with self._lock, self._conn() as c:
            c.execute(
                """
                INSERT INTO documents(
                  id, user_id, name, filename, storage_path, fingerprint,
                  n_rows, n_cols, schema_card, audit, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                  name=excluded.name, filename=excluded.filename,
                  storage_path=excluded.storage_path, fingerprint=excluded.fingerprint,
                  n_rows=excluded.n_rows, n_cols=excluded.n_cols,
                  schema_card=excluded.schema_card, audit=excluded.audit
                """,
                (
                    row["id"],
                    row["user_id"],
                    row["name"],
                    row["filename"],
                    row["storage_path"],
                    row.get("fingerprint"),
                    row.get("n_rows", 0),
                    row.get("n_cols", 0),
                    row.get("schema_card"),
                    json.dumps(row.get("audit") or {}),
                    row.get("created_at") or utcnow(),
                ),
            )

    def list_documents(
        self, user_id: str = "00000000-0000-0000-0000-000000000001"
    ) -> list[dict]:
        with self._lock, self._conn() as c:
            rows = c.execute(
                "SELECT * FROM documents WHERE user_id=? ORDER BY created_at",
                (user_id,),
            ).fetchall()
        out = []
        for r in rows:
            out.append(
                {
                    "id": r["id"],
                    "user_id": r["user_id"],
                    "name": r["name"],
                    "filename": r["filename"],
                    "storage_path": r["storage_path"],
                    "fingerprint": r["fingerprint"],
                    "n_rows": r["n_rows"],
                    "n_cols": r["n_cols"],
                    "schema_card": r["schema_card"],
                    "audit": json.loads(r["audit"] or "{}"),
                    "created_at": r["created_at"],
                }
            )
        return out

    def get_document(self, doc_id: str, user_id: str | None = None) -> dict | None:
        with self._lock, self._conn() as c:
            if user_id:
                row = c.execute(
                    "SELECT * FROM documents WHERE id=? AND user_id=?",
                    (doc_id, user_id),
                ).fetchone()
            else:
                row = c.execute(
                    "SELECT * FROM documents WHERE id=?", (doc_id,)
                ).fetchone()
        if not row:
            return None
        return {
            "id": row["id"],
            "user_id": row["user_id"],
            "name": row["name"],
            "filename": row["filename"],
            "storage_path": row["storage_path"],
            "fingerprint": row["fingerprint"],
            "n_rows": row["n_rows"],
            "n_cols": row["n_cols"],
            "schema_card": row["schema_card"],
            "audit": json.loads(row["audit"] or "{}"),
            "created_at": row["created_at"],
        }

    def delete_document(self, doc_id: str, user_id: str | None = None) -> None:
        with self._lock, self._conn() as c:
            if user_id:
                c.execute(
                    "DELETE FROM documents WHERE id=? AND user_id=?", (doc_id, user_id)
                )
            else:
                c.execute("DELETE FROM documents WHERE id=?", (doc_id,))

    # messages ---------------------------------------------------------------
    def add_message(
        self,
        sid: str,
        role: str,
        content: str,
        meta: dict | None = None,
        user_id: str = "00000000-0000-0000-0000-000000000001",
    ) -> None:
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO messages(session_id, user_id, role, content, meta, created_at)"
                " VALUES(?,?,?,?,?,?)",
                (sid, user_id, role, content, json.dumps(meta or {}), utcnow()),
            )

    def get_messages(self, sid: str, user_id: str | None = None) -> list[dict]:
        with self._lock, self._conn() as c:
            rows = c.execute(
                "SELECT id, role, content, meta, created_at FROM messages"
                " WHERE session_id=? ORDER BY id",
                (sid,),
            ).fetchall()
        return [
            {
                "id": r["id"],
                "role": r["role"],
                "content": r["content"],
                "meta": json.loads(r["meta"] or "{}"),
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    def pop_last_incomplete(self, sid: str, user_id: str | None = None) -> dict | None:
        """Remove and return the latest incomplete assistant message, if any."""
        msgs = self.get_messages(sid, user_id)
        for m in reversed(msgs):
            meta = m.get("meta") or {}
            if m.get("role") == "assistant" and meta.get("incomplete"):
                with self._lock, self._conn() as c:
                    c.execute("DELETE FROM messages WHERE id=?", (m["id"],))
                return m
        return None

    # tool runs --------------------------------------------------------------
    def add_tool_run(
        self,
        sid: str,
        tool: str,
        args: dict | str,
        result: dict | str,
        ok: bool,
        latency_ms: float,
        user_id: str = "00000000-0000-0000-0000-000000000001",
    ) -> None:
        args_s = args if isinstance(args, str) else json.dumps(args, default=str)
        res_s = result if isinstance(result, str) else json.dumps(result, default=str)
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO tool_runs(session_id, user_id, tool, args, result, ok,"
                " latency_ms, created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    sid,
                    user_id,
                    tool,
                    args_s[:4000],
                    res_s[:8000],
                    int(ok),
                    latency_ms,
                    utcnow(),
                ),
            )

    def get_tool_runs(
        self, sid: str, limit: int | None = None, user_id: str | None = None
    ) -> list[dict]:
        q = (
            "SELECT tool, args, result, ok, latency_ms, created_at FROM tool_runs"
            " WHERE session_id=? ORDER BY id"
        )
        if limit:
            q += f" DESC LIMIT {int(limit)}"
        with self._lock, self._conn() as c:
            rows = c.execute(q, (sid,)).fetchall()
        runs = [
            {
                "tool": r["tool"],
                "args": r["args"],
                "result": r["result"],
                "ok": bool(r["ok"]),
                "latency_ms": r["latency_ms"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]
        return list(reversed(runs)) if limit else runs

    # usage --------------------------------------------------------------------
    def add_usage(
        self,
        sid: str,
        prompt_tokens: int,
        completion_tokens: int,
        user_id: str = "00000000-0000-0000-0000-000000000001",
    ) -> None:
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO usage(session_id, user_id, prompt_tokens, completion_tokens,"
                " created_at) VALUES(?,?,?,?,?)",
                (sid, user_id, prompt_tokens, completion_tokens, utcnow()),
            )

    def get_usage(self, sid: str, user_id: str | None = None) -> dict:
        with self._lock, self._conn() as c:
            row = c.execute(
                "SELECT COALESCE(SUM(prompt_tokens),0) AS p,"
                " COALESCE(SUM(completion_tokens),0) AS c,"
                " COUNT(*) AS calls FROM usage WHERE session_id=?",
                (sid,),
            ).fetchone()
        return {
            "prompt_tokens": row["p"],
            "completion_tokens": row["c"],
            "llm_calls": row["calls"],
        }
