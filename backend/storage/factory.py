"""DB factory — Supabase when configured, else SQLite (local/tests)."""

from __future__ import annotations

import logging

from backend.config import settings
from backend.storage.db import DB

log = logging.getLogger("storage")

_db = None


def get_db():
    """Return a process-wide DB instance (SQLite or Supabase)."""
    global _db
    if _db is not None:
        return _db
    if settings.supabase_enabled:
        from backend.storage.supabase_db import SupabaseDB

        log.info("persistence: Supabase Postgres (%s)", settings.supabase_url)
        _db = SupabaseDB()
    else:
        log.info("persistence: SQLite (%s)", settings.db_path)
        _db = DB(settings.db_path)
    return _db


def reset_db_for_tests(db) -> None:
    """Swap the process-wide DB (used by pytest)."""
    global _db
    _db = db
