"""FastAPI backend: multi-doc library, session attachments, SSE chat, Supabase auth."""

from __future__ import annotations

import hashlib
import json
import logging
import queue
import re
import threading
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse

from backend.agent.core import CONTINUE_NUDGE, Agent, deserialize_contents
from backend.auth.deps import LOCAL_USER_ID, UserDep
from backend.config import settings
from backend.docs.store import store
from backend.llm.client import LLMClient
from backend.logging_conf import setup_logging
from backend.schemas import (
    AuthCredentials,
    ChatMessageIn,
    DatasetInfo,
    DocAttachIn,
    SessionDocsIn,
    SessionInfo,
    SessionSummary,
)
from backend.storage.db import DB
from backend.storage.factory import get_db
from backend.tools.code_runner import CodeRunner
from backend.tools.search_tool import SearchTool

setup_logging(settings.log_level)
log = logging.getLogger("api")

app = FastAPI(title="Inventory Q&A Agent", version="1.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

db = get_db()
store.bind_db(db)
llm = LLMClient()
runner = CodeRunner()
search = SearchTool(llm)

# session_id -> Event set by POST /cancel so the agent loop can stop mid-turn
_cancel_flags: dict[str, threading.Event] = {}
_cancel_lock = threading.Lock()


def _register_cancel(session_id: str) -> threading.Event:
    ev = threading.Event()
    with _cancel_lock:
        old = _cancel_flags.get(session_id)
        if old:
            old.set()
        _cancel_flags[session_id] = ev
    return ev


def _clear_cancel(session_id: str, ev: threading.Event) -> None:
    with _cancel_lock:
        if _cancel_flags.get(session_id) is ev:
            _cancel_flags.pop(session_id, None)

_MENTION_RE = re.compile(r"@([A-Za-z0-9_-]+)")


@app.on_event("startup")
def load_default_dataset() -> None:
    # Seed the sample workbook only in local/dev mode (no Supabase).
    if settings.supabase_enabled:
        log.info("Supabase mode — skipping default dataset seed")
        return
    uid = LOCAL_USER_ID
    if settings.default_dataset.exists() and not store.list(uid):
        rec = store.add(settings.default_dataset, user_id=uid)
        log.info(
            "default document loaded: id=%s name=%s rows=%s",
            rec.id,
            rec.name,
            len(rec.tool.df),
        )


def _require_session(session_id: str, user_id: str) -> None:
    if not db.session_exists(session_id, user_id):
        raise HTTPException(404, "session not found")


def _session_docs(session_id: str, user_id: str):
    ids = db.get_session_docs(session_id, user_id)
    return store.resolve_many(ids, user_id)


def _parse_mentions(question: str) -> list[str]:
    return list(dict.fromkeys(_MENTION_RE.findall(question or "")))


def build_agent(
    session_id: str,
    user_id: str,
    on_event=None,
    model: str | None = None,
    question: str = "",
    focus_ids: list[str] | None = None,
    cancel_event: threading.Event | None = None,
) -> Agent:
    client = llm.for_model(model) if model else llm
    attached = _session_docs(session_id, user_id)

    for name in _parse_mentions(question):
        rec = store.get_by_name(name, user_id)
        if rec and rec.id not in {d.id for d in attached}:
            db.attach_session_doc(session_id, rec.id, user_id)
            attached = _session_docs(session_id, user_id)

    if focus_ids:
        focus = store.resolve_many(focus_ids, user_id)
        attached_ids = {d.id for d in attached}
        focus = [d for d in focus if d.id in attached_ids]
        if focus:
            attached = focus

    primary = None
    mentions = _parse_mentions(question)
    if mentions:
        for name in mentions:
            for d in attached:
                if d.name.lower() == name.lower():
                    primary = d.name
                    break
            if primary:
                break
    if not primary and attached:
        primary = attached[0].name

    return Agent(
        client,
        code_runner=runner,
        search_tool=search,
        db=db,
        session_id=session_id,
        user_id=user_id,
        on_event=on_event,
        docs=attached,
        primary=primary,
        cancel_event=cancel_event,
    )


def load_history_contents(session_id: str, user_id: str) -> list:
    from google.genai import types

    contents = []
    for msg in db.get_messages(session_id, user_id):
        if msg["role"] not in ("user", "assistant", "model"):
            continue
        role = "user" if msg["role"] == "user" else "model"
        contents.append(
            types.Content(role=role, parts=[types.Part(text=msg["content"])])
        )
    return contents


# --------------------------------------------------------------------- health
@app.get("/health")
def health():
    """Public liveness probe — no auth required."""
    return {
        "status": "ok",
        "model": settings.model_name,
        "models": list(settings.selectable_models),
        "auth": "supabase" if settings.supabase_enabled else "local",
        "persistence": getattr(db, "backend", "sqlite"),
    }


@app.get("/auth/config")
def auth_config():
    """Public: tells the SPA whether to show the login gate."""
    return {
        "auth_required": settings.supabase_enabled,
        "supabase_url": settings.supabase_url if settings.supabase_enabled else "",
        "supabase_anon_key": (
            settings.supabase_anon_key if settings.supabase_enabled else ""
        ),
    }


def _supabase_auth_headers() -> dict[str, str]:
    key = settings.supabase_anon_key or settings.supabase_service_role_key
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }


def _proxy_supabase_auth(path: str, body: dict) -> dict:
    """Same-origin auth proxy so the SPA never calls Supabase Auth directly."""
    if not settings.supabase_enabled:
        raise HTTPException(400, "auth not configured")
    import httpx

    url = settings.supabase_url.rstrip("/") + "/auth/v1/" + path
    try:
        r = httpx.post(url, headers=_supabase_auth_headers(), json=body, timeout=20.0)
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"auth upstream failed: {exc}") from exc
    data = {}
    try:
        data = r.json()
    except Exception:
        data = {}
    if r.status_code >= 400:
        msg = (
            data.get("error_description")
            or data.get("msg")
            or data.get("error")
            or data.get("message")
            or r.text[:200]
            or r.reason_phrase
        )
        raise HTTPException(r.status_code, str(msg))
    return data


@app.post("/auth/login")
def auth_login(body: AuthCredentials):
    return _proxy_supabase_auth(
        "token?grant_type=password",
        {"email": body.email.strip(), "password": body.password},
    )


@app.post("/auth/signup")
def auth_signup(body: AuthCredentials):
    return _proxy_supabase_auth(
        "signup",
        {"email": body.email.strip(), "password": body.password},
    )


@app.get("/auth/me")
def auth_me(user: UserDep):
    return {"id": user.id, "email": user.email, "role": user.role}


@app.get("/models")
def list_models(user: UserDep):
    return {
        "default": settings.model_name,
        "models": [
            {
                "id": m,
                "label": m.replace("gemini-", "").replace("-", " "),
                "tier": "lite" if "lite" in m else "flash",
            }
            for m in settings.selectable_models
        ],
    }


# ----------------------------------------------------------------- documents
@app.get("/documents")
def list_documents(user: UserDep):
    return [d.summary() for d in store.list(user.id)]


@app.post("/documents")
async def upload_document(user: UserDep, file: UploadFile = File(...)):  # noqa: B008
    suffix = Path(file.filename or "upload.xlsx").suffix.lower()
    if suffix not in (".xlsx", ".xls"):
        raise HTTPException(400, "only .xlsx / .xls files are supported")
    dest = (
        settings.uploads_dir
        / f"{user.id[:8]}_{hashlib.md5(file.filename.encode()).hexdigest()[:8]}_{file.filename}"
    )
    dest.write_bytes(await file.read())
    try:
        rec = store.add(dest, display_name=file.filename, user_id=user.id)
    except Exception as exc:
        raise HTTPException(400, f"could not parse workbook: {exc}") from exc
    log.info("document added: %s (%s rows) user=%s", rec.name, len(rec.tool.df), user.id)
    return rec.summary()


@app.delete("/documents/{doc_id}")
def delete_document(doc_id: str, user: UserDep):
    if not store.get(doc_id, user.id):
        raise HTTPException(404, "document not found")
    store.remove(doc_id, user.id)
    return {"ok": True}


@app.get("/documents/{doc_id}/preview")
def document_preview(doc_id: str, user: UserDep, rows: int = 15):
    rec = store.get(doc_id, user.id)
    if not rec:
        raise HTTPException(404, "document not found")
    show = rec.tool.df.head(max(1, min(rows, 50)))
    return {
        "id": rec.id,
        "name": rec.name,
        "filename": rec.filename,
        "columns": list(show.columns),
        "rows": show.astype(object).where(show.notna(), None).to_dict(orient="records"),
        "total_rows": int(len(rec.tool.df)),
        "audit": rec.tool.audit,
        "schema_card": rec.tool.schema_card,
        "shape": list(rec.tool.df.shape),
    }


@app.get("/dataset", response_model=DatasetInfo)
def dataset_info(user: UserDep, doc_id: str | None = None):
    docs = store.list(user.id)
    if not docs:
        raise HTTPException(503, "no documents loaded yet")
    rec = store.get(doc_id, user.id) if doc_id else docs[0]
    if not rec:
        raise HTTPException(404, "document not found")
    return DatasetInfo(
        file=f"@{rec.name} · {rec.filename}",
        shape=list(rec.tool.df.shape),
        schema_card=rec.tool.schema_card,
        audit=rec.tool.audit,
    )


@app.post("/dataset", response_model=DatasetInfo)
async def upload_dataset_compat(user: UserDep, file: UploadFile = File(...)):  # noqa: B008
    summary = await upload_document(user, file)
    return DatasetInfo(
        file=f"@{summary['name']} · {summary['filename']}",
        shape=[summary["rows"], summary["columns"]],
        schema_card=summary["schema_card"],
        audit=summary["audit"],
    )


@app.get("/dataset/preview")
def dataset_preview(user: UserDep, rows: int = 15, doc_id: str | None = None):
    docs = store.list(user.id)
    if not docs:
        raise HTTPException(503, "no documents loaded yet")
    rec = store.get(doc_id, user.id) if doc_id else docs[0]
    if not rec:
        raise HTTPException(404, "document not found")
    show = rec.tool.df.head(max(1, min(rows, 50)))
    return {
        "id": rec.id,
        "name": rec.name,
        "columns": list(show.columns),
        "rows": show.astype(object).where(show.notna(), None).to_dict(orient="records"),
        "total_rows": int(len(rec.tool.df)),
    }


# -------------------------------------------------------------------- sessions
@app.get("/sessions", response_model=list[SessionSummary])
def list_sessions(user: UserDep, limit: int = 40):
    return [SessionSummary(**row) for row in db.list_sessions(user.id, limit=limit)]


@app.post("/sessions", response_model=SessionInfo)
def create_session(user: UserDep):
    return SessionInfo(session_id=db.create_session(user.id))


@app.get("/sessions/{session_id}/documents")
def get_session_documents(session_id: str, user: UserDep):
    _require_session(session_id, user.id)
    docs = _session_docs(session_id, user.id)
    return {
        "session_id": session_id,
        "doc_ids": [d.id for d in docs],
        "documents": [d.summary() for d in docs],
    }


@app.put("/sessions/{session_id}/documents")
def set_session_documents(session_id: str, body: SessionDocsIn, user: UserDep):
    _require_session(session_id, user.id)
    valid = []
    for did in body.doc_ids:
        if not store.get(did, user.id):
            raise HTTPException(400, f"unknown document id: {did}")
        valid.append(did)
    db.set_session_docs(session_id, valid, user.id)
    return get_session_documents(session_id, user)


@app.post("/sessions/{session_id}/documents")
def attach_session_document(session_id: str, body: DocAttachIn, user: UserDep):
    _require_session(session_id, user.id)
    if not store.get(body.doc_id, user.id):
        raise HTTPException(404, "document not found")
    db.attach_session_doc(session_id, body.doc_id, user.id)
    return get_session_documents(session_id, user)


@app.delete("/sessions/{session_id}/documents/{doc_id}")
def detach_session_document(session_id: str, doc_id: str, user: UserDep):
    _require_session(session_id, user.id)
    db.detach_session_doc(session_id, doc_id, user.id)
    return get_session_documents(session_id, user)


@app.get("/sessions/{session_id}/messages")
def get_messages(session_id: str, user: UserDep):
    _require_session(session_id, user.id)
    return db.get_messages(session_id, user.id)


@app.delete("/sessions/{session_id}")
def delete_session(session_id: str, user: UserDep):
    _require_session(session_id, user.id)
    db.delete_session(session_id, user.id)
    return {"ok": True}


@app.get("/sessions/{session_id}/notebook")
def export_notebook(session_id: str, user: UserDep):
    _require_session(session_id, user.id)
    cells = [
        {
            "cell_type": "markdown",
            "metadata": {},
            "source": f"# Inventory Q&A session `{session_id}`\n\nExported conversation with executed code.",
        }
    ]
    for msg in db.get_messages(session_id, user.id):
        cells.append(
            {
                "cell_type": "markdown",
                "metadata": {},
                "source": (
                    f"**{'Question' if msg['role'] == 'user' else 'Answer'}:**\n\n"
                    + msg["content"]
                ),
            }
        )
    for run in db.get_tool_runs(session_id, user_id=user.id):
        if run["tool"] != "run_dataframe_code":
            continue
        try:
            args = json.loads(run["args"])
            result = json.loads(run["result"])
        except json.JSONDecodeError:
            continue
        cells.append(
            {
                "cell_type": "code",
                "metadata": {},
                "execution_count": None,
                "outputs": [
                    {
                        "output_type": "stream",
                        "name": "stdout",
                        "text": str(
                            result.get("result_repr") or result.get("error") or ""
                        ),
                    }
                ],
                "source": args.get("code", ""),
            }
        )
    notebook = {
        "cells": cells,
        "metadata": {
            "kernelspec": {
                "display_name": "Python 3",
                "language": "python",
                "name": "python3",
            }
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }
    out_path = Path(settings.uploads_dir) / f"session_{session_id}.ipynb"
    out_path.write_text(json.dumps(notebook, indent=2, default=str))
    return FileResponse(out_path, filename=f"session_{session_id}.ipynb")


# ----------------------------------------------------------------------- chat
@app.post("/sessions/{session_id}/cancel")
def cancel_generation(session_id: str, user: UserDep):
    """Stop the in-flight agent loop for this session (GPT-style Stop)."""
    _require_session(session_id, user.id)
    with _cancel_lock:
        ev = _cancel_flags.get(session_id)
    if ev:
        ev.set()
        return {"ok": True, "cancelled": True}
    return {"ok": True, "cancelled": False}


@app.post("/sessions/{session_id}/messages")
def post_message(
    session_id: str, body: ChatMessageIn, user: UserDep, stream: bool = True
):
    _require_session(session_id, user.id)
    try:
        model = settings.resolve_model(body.model)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    resume = bool(body.continue_from_incomplete)
    question = (body.question or "").strip()
    history = None
    ask_question = question
    resume_flag = False

    if resume:
        incomplete = db.pop_last_incomplete(session_id, user.id)
        if not incomplete:
            raise HTTPException(400, "no incomplete answer to continue")
        meta = incomplete.get("meta") or {}
        resume_contents = meta.get("resume_contents")
        if not resume_contents:
            raise HTTPException(400, "incomplete turn has no resumable state")
        history = deserialize_contents(resume_contents)
        ask_question = CONTINUE_NUDGE
        resume_flag = True
        # Prefer the original question for @mention / focus resolution
        for msg in reversed(db.get_messages(session_id, user.id)):
            if msg.get("role") == "user" and (msg.get("content") or "").strip():
                question = msg["content"]
                break
    else:
        if not question:
            raise HTTPException(400, "question is required")
        db.add_message(session_id, "user", question, user_id=user.id)
        history = load_history_contents(session_id, user.id)[:-1]
        ask_question = question

    cancel_ev = _register_cancel(session_id)

    if not stream:
        try:
            agent = build_agent(
                session_id,
                user.id,
                model=model,
                question=question,
                focus_ids=body.doc_ids,
                cancel_event=cancel_ev,
            )
            answer = agent.ask(ask_question, history=history, resume=resume_flag)
            return answer.model_dump()
        finally:
            _clear_cancel(session_id, cancel_ev)

    event_queue: queue.Queue = queue.Queue()
    uid = user.id

    def run_agent():
        try:
            agent = build_agent(
                session_id,
                uid,
                on_event=lambda ev, d: event_queue.put((ev, d)),
                model=model,
                question=question,
                focus_ids=body.doc_ids,
                cancel_event=cancel_ev,
            )
            answer = agent.ask(ask_question, history=history, resume=resume_flag)
            event_queue.put(("answer", answer.model_dump()))
        except Exception as exc:
            log.exception("agent failed")
            event_queue.put(("error", {"message": str(exc)}))
        finally:
            _clear_cancel(session_id, cancel_ev)
        event_queue.put(None)

    def event_stream():
        threading.Thread(target=run_agent, daemon=True).start()
        while True:
            item = event_queue.get()
            if item is None:
                break
            event, data = item
            yield f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# -------------------------------------------------------------------- frontend
from fastapi.staticfiles import StaticFiles  # noqa: E402

app.mount(
    "/app",
    StaticFiles(directory=Path(__file__).resolve().parent.parent / "frontend", html=True),
    name="frontend",
)
