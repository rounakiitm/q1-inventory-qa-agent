"""User-scoped document library with SQLite/Supabase persistence.

In-memory DataTool frames are cached per user; metadata + file bytes live in
the DB (and Supabase Storage when enabled).
"""

from __future__ import annotations

import logging
import re
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

from backend.config import settings
from backend.tools.data_tool import DataTool

log = logging.getLogger("docs")


def _slug(name: str) -> str:
    base = Path(name).stem
    s = re.sub(r"[^a-zA-Z0-9_-]+", "_", base).strip("_")
    return (s or "doc")[:40]


@dataclass
class DocRecord:
    id: str
    name: str
    filename: str
    path: Path
    tool: DataTool
    user_id: str = ""
    storage_path: str = ""

    def summary(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "filename": self.filename,
            "rows": int(len(self.tool.df)),
            "columns": int(self.tool.df.shape[1]),
            "fingerprint": self.tool.fingerprint,
            "schema_card": self.tool.schema_card,
            "audit": self.tool.audit,
        }


class DocumentStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        # user_id -> doc_id -> DocRecord
        self._docs: dict[str, dict[str, DocRecord]] = {}
        # user_id -> lower name -> doc_id
        self._names: dict[str, dict[str, str]] = {}
        self._hydrated: set[str] = set()
        self._db = None

    def bind_db(self, db) -> None:
        self._db = db

    def _bucket(self, user_id: str) -> dict[str, DocRecord]:
        return self._docs.setdefault(user_id, {})

    def _name_map(self, user_id: str) -> dict[str, str]:
        return self._names.setdefault(user_id, {})

    def ensure_loaded(self, user_id: str) -> None:
        """Hydrate this user's docs from DB into the in-memory cache once."""
        if user_id in self._hydrated or self._db is None:
            return
        with self._lock:
            if user_id in self._hydrated:
                return
            try:
                rows = self._db.list_documents(user_id)
            except Exception:
                log.exception("failed listing documents for %s", user_id)
                self._hydrated.add(user_id)
                return
            for row in rows:
                try:
                    path = self._resolve_path(row)
                    if path is None or not path.exists():
                        log.warning("missing file for doc %s", row.get("id"))
                        continue
                    tool = DataTool(path)
                    rec = DocRecord(
                        id=row["id"],
                        name=row["name"],
                        filename=row["filename"],
                        path=path,
                        tool=tool,
                        user_id=user_id,
                        storage_path=row.get("storage_path") or "",
                    )
                    self._bucket(user_id)[rec.id] = rec
                    self._name_map(user_id)[rec.name.lower()] = rec.id
                except Exception:
                    log.exception("hydrate doc %s failed", row.get("id"))
            self._hydrated.add(user_id)

    def _resolve_path(self, row: dict) -> Path | None:
        """Local path, or download from Supabase Storage into uploads cache."""
        storage_path = row.get("storage_path") or ""
        local = Path(storage_path)
        if local.is_file():
            return local
        # Relative under uploads
        candidate = settings.uploads_dir / Path(storage_path).name
        if candidate.is_file():
            return candidate
        if getattr(self._db, "backend", "") == "supabase" and storage_path:
            try:
                from backend.storage.supabase_client import get_supabase

                data = get_supabase().storage_download(storage_path)
                dest = settings.uploads_dir / f"{row['id']}_{row['filename']}"
                dest.write_bytes(data)
                return dest
            except Exception:
                log.exception("download %s failed", storage_path)
                return None
        return None

    def add(
        self,
        path: Path,
        display_name: str | None = None,
        user_id: str = "",
        *,
        persist: bool = True,
    ) -> DocRecord:
        path = Path(path)
        tool = DataTool(path)
        raw = display_name or path.name
        base = _slug(raw)
        with self._lock:
            names = self._name_map(user_id)
            name = base
            n = 2
            while name.lower() in names:
                name = f"{base}_{n}"
                n += 1
            doc_id = uuid.uuid4().hex[:10]
            storage_path = str(path)
            # Upload bytes to Supabase Storage when in prod mode.
            if persist and self._db is not None and getattr(self._db, "backend", "") == "supabase":
                try:
                    from backend.storage.supabase_client import get_supabase

                    remote = f"{user_id}/{doc_id}_{path.name}"
                    get_supabase().storage_upload(
                        remote,
                        path.read_bytes(),
                        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    )
                    storage_path = remote
                except Exception:
                    log.exception("storage upload failed; keeping local path")

            rec = DocRecord(
                id=doc_id,
                name=name,
                filename=path.name,
                path=path,
                tool=tool,
                user_id=user_id,
                storage_path=storage_path,
            )
            self._bucket(user_id)[doc_id] = rec
            names[name.lower()] = doc_id
            self._hydrated.add(user_id)

            if persist and self._db is not None:
                self._db.upsert_document(
                    {
                        "id": doc_id,
                        "user_id": user_id,
                        "name": name,
                        "filename": path.name,
                        "storage_path": storage_path,
                        "fingerprint": tool.fingerprint,
                        "n_rows": int(len(tool.df)),
                        "n_cols": int(tool.df.shape[1]),
                        "schema_card": tool.schema_card,
                        "audit": tool.audit,
                    }
                )
            return rec

    def clear(self, user_id: str | None = None) -> None:
        with self._lock:
            if user_id is None:
                self._docs.clear()
                self._names.clear()
                self._hydrated.clear()
            else:
                self._docs.pop(user_id, None)
                self._names.pop(user_id, None)
                self._hydrated.discard(user_id)

    def list(self, user_id: str = "") -> list[DocRecord]:
        self.ensure_loaded(user_id)
        with self._lock:
            return list(self._bucket(user_id).values())

    def get(self, doc_id: str, user_id: str = "") -> DocRecord | None:
        self.ensure_loaded(user_id)
        with self._lock:
            return self._bucket(user_id).get(doc_id)

    def get_by_name(self, name: str, user_id: str = "") -> DocRecord | None:
        self.ensure_loaded(user_id)
        with self._lock:
            doc_id = self._name_map(user_id).get(name.lower().lstrip("@"))
            return self._bucket(user_id).get(doc_id) if doc_id else None

    def remove(self, doc_id: str, user_id: str = "") -> bool:
        with self._lock:
            bucket = self._bucket(user_id)
            rec = bucket.pop(doc_id, None)
            if not rec:
                return False
            self._name_map(user_id).pop(rec.name.lower(), None)
        if self._db is not None:
            try:
                if getattr(self._db, "backend", "") == "supabase" and rec.storage_path:
                    from backend.storage.supabase_client import get_supabase

                    get_supabase().storage_delete(rec.storage_path)
            except Exception:
                log.exception("storage delete failed")
            self._db.delete_document(doc_id, user_id)
            self._db.detach_doc_everywhere(doc_id, user_id)
        return True

    def resolve_many(self, ids: list[str], user_id: str = "") -> list[DocRecord]:
        self.ensure_loaded(user_id)
        out: list[DocRecord] = []
        with self._lock:
            bucket = self._bucket(user_id)
            for i in ids:
                rec = bucket.get(i)
                if rec:
                    out.append(rec)
        return out


store = DocumentStore()
