"""Supabase Postgres persistence — same surface as SQLite DB, always user-scoped."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from backend.storage.supabase_client import SupabaseREST, get_supabase

log = logging.getLogger("supabase_db")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _eq(val: str) -> str:
    """PostgREST equality filter value."""
    return f"eq.{val}"


class SupabaseDB:
    backend = "supabase"

    def __init__(self, client: SupabaseREST | None = None):
        self.sb = client or get_supabase()

    # sessions -------------------------------------------------------------
    def create_session(self, user_id: str) -> str:
        sid = uuid.uuid4().hex[:12]
        self.sb.insert("sessions", {"id": sid, "user_id": user_id})
        return sid

    def session_exists(self, sid: str, user_id: str | None = None) -> bool:
        filters: dict[str, str] = {"id": _eq(sid)}
        if user_id:
            filters["user_id"] = _eq(user_id)
        rows = self.sb.select("sessions", columns="id", filters=filters, limit=1)
        return bool(rows)

    def list_sessions(self, user_id: str, limit: int = 40) -> list[dict]:
        """Two round-trips total (sessions + batched messages) — not N+1."""
        lim = max(1, min(int(limit), 100))
        sessions = (
            self.sb.select(
                "sessions",
                columns="id,created_at",
                filters={"user_id": _eq(user_id)},
                order="created_at.desc",
                limit=lim,
            )
            or []
        )
        if not sessions:
            return []

        ids = [s["id"] for s in sessions]
        # PostgREST: session_id=in.(a,b,c)
        in_list = "(" + ",".join(ids) + ")"
        msgs = (
            self.sb.select(
                "messages",
                columns="session_id,role,content,created_at",
                filters={
                    "user_id": _eq(user_id),
                    "session_id": f"in.{in_list}",
                },
                order="id.asc",
            )
            or []
        )

        by_sid: dict[str, list[dict]] = {sid: [] for sid in ids}
        for m in msgs:
            sid = m.get("session_id")
            if sid in by_sid:
                by_sid[sid].append(m)

        out: list[dict] = []
        for s in sessions:
            sid = s["id"]
            sm = by_sid.get(sid) or []
            preview = "New session"
            last_activity = s["created_at"]
            for m in sm:
                if m.get("role") == "user" and preview == "New session":
                    preview = (m.get("content") or "New session")[:120]
                if m.get("created_at"):
                    last_activity = m["created_at"]
            out.append(
                {
                    "session_id": sid,
                    "created_at": s["created_at"],
                    "last_activity": last_activity,
                    "n_messages": len(sm),
                    "preview": preview,
                }
            )
        out.sort(key=lambda r: r["last_activity"] or r["created_at"], reverse=True)
        return out[:lim]

    def delete_session(self, sid: str, user_id: str | None = None) -> None:
        if user_id and not self.session_exists(sid, user_id):
            return
        # Cascades handle children when FK is set; delete children explicitly too.
        for table in ("messages", "tool_runs", "usage", "session_docs"):
            self.sb.delete(table, {"session_id": _eq(sid)})
        filters: dict[str, str] = {"id": _eq(sid)}
        if user_id:
            filters["user_id"] = _eq(user_id)
        self.sb.delete("sessions", filters)

    # session documents ----------------------------------------------------
    def set_session_docs(
        self, sid: str, doc_ids: list[str], user_id: str = ""
    ) -> None:
        self.sb.delete("session_docs", {"session_id": _eq(sid)})
        if not doc_ids:
            return
        rows = [
            {"session_id": sid, "doc_id": did, "user_id": user_id} for did in doc_ids
        ]
        self.sb.insert("session_docs", rows)

    def attach_session_doc(
        self, sid: str, doc_id: str, user_id: str = ""
    ) -> None:
        try:
            self.sb.insert(
                "session_docs",
                {"session_id": sid, "doc_id": doc_id, "user_id": user_id},
            )
        except RuntimeError as exc:
            # unique conflict — already attached
            if "409" not in str(exc) and "duplicate" not in str(exc).lower():
                raise

    def detach_session_doc(
        self, sid: str, doc_id: str, user_id: str | None = None
    ) -> None:
        filters = {"session_id": _eq(sid), "doc_id": _eq(doc_id)}
        if user_id:
            filters["user_id"] = _eq(user_id)
        self.sb.delete("session_docs", filters)

    def get_session_docs(self, sid: str, user_id: str | None = None) -> list[str]:
        filters: dict[str, str] = {"session_id": _eq(sid)}
        if user_id:
            filters["user_id"] = _eq(user_id)
        rows = (
            self.sb.select(
                "session_docs",
                columns="doc_id",
                filters=filters,
                order="doc_id.asc",
            )
            or []
        )
        return [r["doc_id"] for r in rows]

    def detach_doc_everywhere(self, doc_id: str, user_id: str | None = None) -> None:
        filters: dict[str, str] = {"doc_id": _eq(doc_id)}
        if user_id:
            filters["user_id"] = _eq(user_id)
        self.sb.delete("session_docs", filters)

    # documents ------------------------------------------------------------
    def upsert_document(self, row: dict) -> None:
        payload = {
            "id": row["id"],
            "user_id": row["user_id"],
            "name": row["name"],
            "filename": row["filename"],
            "storage_path": row["storage_path"],
            "fingerprint": row.get("fingerprint"),
            "n_rows": row.get("n_rows", 0),
            "n_cols": row.get("n_cols", 0),
            "schema_card": row.get("schema_card"),
            "audit": row.get("audit") or {},
            "created_at": row.get("created_at") or utcnow(),
        }
        self.sb.upsert("documents", payload, on_conflict="id")

    def list_documents(self, user_id: str) -> list[dict]:
        rows = (
            self.sb.select(
                "documents",
                filters={"user_id": _eq(user_id)},
                order="created_at.asc",
            )
            or []
        )
        return [self._doc_row(r) for r in rows]

    def get_document(self, doc_id: str, user_id: str | None = None) -> dict | None:
        filters: dict[str, str] = {"id": _eq(doc_id)}
        if user_id:
            filters["user_id"] = _eq(user_id)
        rows = self.sb.select("documents", filters=filters, limit=1) or []
        return self._doc_row(rows[0]) if rows else None

    def delete_document(self, doc_id: str, user_id: str | None = None) -> None:
        filters: dict[str, str] = {"id": _eq(doc_id)}
        if user_id:
            filters["user_id"] = _eq(user_id)
        self.sb.delete("documents", filters)

    @staticmethod
    def _doc_row(r: dict) -> dict:
        audit = r.get("audit") or {}
        if isinstance(audit, str):
            try:
                audit = json.loads(audit)
            except json.JSONDecodeError:
                audit = {}
        return {
            "id": r["id"],
            "user_id": r["user_id"],
            "name": r["name"],
            "filename": r["filename"],
            "storage_path": r["storage_path"],
            "fingerprint": r.get("fingerprint"),
            "n_rows": r.get("n_rows", 0),
            "n_cols": r.get("n_cols", 0),
            "schema_card": r.get("schema_card"),
            "audit": audit,
            "created_at": r.get("created_at"),
        }

    # messages ---------------------------------------------------------------
    def add_message(
        self,
        sid: str,
        role: str,
        content: str,
        meta: dict | None = None,
        user_id: str = "",
    ) -> None:
        self.sb.insert(
            "messages",
            {
                "session_id": sid,
                "user_id": user_id,
                "role": role,
                "content": content,
                "meta": meta or {},
            },
        )

    def get_messages(self, sid: str, user_id: str | None = None) -> list[dict]:
        filters: dict[str, str] = {"session_id": _eq(sid)}
        if user_id:
            filters["user_id"] = _eq(user_id)
        rows = (
            self.sb.select(
                "messages",
                columns="id,role,content,meta,created_at",
                filters=filters,
                order="id.asc",
            )
            or []
        )
        out = []
        for r in rows:
            meta = r.get("meta") or {}
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except json.JSONDecodeError:
                    meta = {}
            out.append(
                {
                    "id": r.get("id"),
                    "role": r["role"],
                    "content": r["content"],
                    "meta": meta,
                    "created_at": r.get("created_at"),
                }
            )
        return out

    def pop_last_incomplete(self, sid: str, user_id: str | None = None) -> dict | None:
        """Remove and return the latest incomplete assistant message, if any."""
        msgs = self.get_messages(sid, user_id)
        for m in reversed(msgs):
            meta = m.get("meta") or {}
            if m.get("role") == "assistant" and meta.get("incomplete") and m.get("id") is not None:
                self.sb.delete("messages", {"id": f"eq.{m['id']}"})
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
        user_id: str = "",
    ) -> None:
        args_s = args if isinstance(args, str) else json.dumps(args, default=str)
        res_s = result if isinstance(result, str) else json.dumps(result, default=str)
        self.sb.insert(
            "tool_runs",
            {
                "session_id": sid,
                "user_id": user_id,
                "tool": tool,
                "args": args_s[:4000],
                "result": res_s[:8000],
                "ok": bool(ok),
                "latency_ms": latency_ms,
            },
        )

    def get_tool_runs(
        self, sid: str, limit: int | None = None, user_id: str | None = None
    ) -> list[dict]:
        filters: dict[str, str] = {"session_id": _eq(sid)}
        if user_id:
            filters["user_id"] = _eq(user_id)
        order = "id.desc" if limit else "id.asc"
        rows = (
            self.sb.select(
                "tool_runs",
                columns="tool,args,result,ok,latency_ms,created_at",
                filters=filters,
                order=order,
                limit=limit,
            )
            or []
        )
        runs = [
            {
                "tool": r["tool"],
                "args": r["args"],
                "result": r["result"],
                "ok": bool(r["ok"]),
                "latency_ms": r["latency_ms"],
                "created_at": r.get("created_at"),
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
        user_id: str = "",
    ) -> None:
        self.sb.insert(
            "usage",
            {
                "session_id": sid,
                "user_id": user_id,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
            },
        )

    def get_usage(self, sid: str, user_id: str | None = None) -> dict:
        filters: dict[str, str] = {"session_id": _eq(sid)}
        if user_id:
            filters["user_id"] = _eq(user_id)
        rows = (
            self.sb.select(
                "usage",
                columns="prompt_tokens,completion_tokens",
                filters=filters,
            )
            or []
        )
        p = sum(int(r.get("prompt_tokens") or 0) for r in rows)
        c = sum(int(r.get("completion_tokens") or 0) for r in rows)
        return {"prompt_tokens": p, "completion_tokens": c, "llm_calls": len(rows)}
