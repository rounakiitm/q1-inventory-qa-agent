"""Minimal Supabase REST client (PostgREST + Storage) via httpx.

Uses the service-role key so the backend can enforce user_id scoping itself.
Keeps a persistent httpx.Client so TLS isn't renegotiated every call.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any

import httpx

from backend.config import settings

log = logging.getLogger("supabase")

BUCKET = "workbooks"


class SupabaseREST:
    def __init__(self, url: str, service_key: str):
        self.base = url.rstrip("/")
        self.key = service_key
        self._headers = {
            "apikey": service_key,
            "Authorization": f"Bearer {service_key}",
            "Content-Type": "application/json",
        }
        self._client = httpx.Client(timeout=30.0, headers=self._headers)
        self._storage_client = httpx.Client(timeout=60.0)

    def close(self) -> None:
        self._client.close()
        self._storage_client.close()

    def _rest(
        self,
        method: str,
        table: str,
        *,
        params: dict | None = None,
        json_body: Any = None,
        prefer: str | None = None,
        headers_extra: dict | None = None,
    ) -> Any:
        url = f"{self.base}/rest/v1/{table}"
        headers: dict[str, str] = {}
        if prefer:
            headers["Prefer"] = prefer
        if headers_extra:
            headers.update(headers_extra)
        r = self._client.request(
            method, url, params=params, json=json_body, headers=headers or None
        )
        if r.status_code >= 400:
            raise RuntimeError(
                f"supabase {method} {table}: {r.status_code} {r.text[:400]}"
            )
        if r.status_code == 204 or not r.content:
            return None
        return r.json()

    def select(
        self,
        table: str,
        *,
        columns: str = "*",
        filters: dict | None = None,
        order: str | None = None,
        limit: int | None = None,
        single: bool = False,
    ) -> Any:
        params: dict[str, Any] = {"select": columns}
        if filters:
            params.update(filters)
        if order:
            params["order"] = order
        if limit is not None:
            params["limit"] = str(limit)
        prefer = "return=representation"
        headers = {"Accept": "application/vnd.pgrst.object+json"} if single else None
        return self._rest(
            "GET", table, params=params, prefer=prefer, headers_extra=headers
        )

    def insert(self, table: str, rows: dict | list[dict]) -> Any:
        return self._rest(
            "POST", table, json_body=rows, prefer="return=representation"
        )

    def upsert(
        self, table: str, rows: dict | list[dict], on_conflict: str = "id"
    ) -> Any:
        url = f"{self.base}/rest/v1/{table}"
        headers = {"Prefer": "resolution=merge-duplicates,return=representation"}
        r = self._client.post(
            url,
            params={"on_conflict": on_conflict},
            json=rows,
            headers=headers,
        )
        if r.status_code >= 400:
            raise RuntimeError(
                f"supabase upsert {table}: {r.status_code} {r.text[:400]}"
            )
        if r.status_code == 204 or not r.content:
            return None
        return r.json()

    def update(self, table: str, filters: dict, patch: dict) -> Any:
        return self._rest(
            "PATCH",
            table,
            params=filters,
            json_body=patch,
            prefer="return=representation",
        )

    def delete(self, table: str, filters: dict) -> None:
        self._rest("DELETE", table, params=filters, prefer="return=minimal")

    def rpc(self, fn: str, args: dict) -> Any:
        return self._rest("POST", f"rpc/{fn}", json_body=args)

    # ---- Storage ----------------------------------------------------------
    def storage_upload(
        self,
        path: str,
        data: bytes,
        content_type: str = "application/octet-stream",
    ) -> str:
        url = f"{self.base}/storage/v1/object/{BUCKET}/{path}"
        headers = {
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
            "Content-Type": content_type,
            "x-upsert": "true",
        }
        r = self._storage_client.post(url, content=data, headers=headers)
        if r.status_code >= 400:
            raise RuntimeError(
                f"storage upload failed: {r.status_code} {r.text[:300]}"
            )
        return path

    def storage_download(self, path: str) -> bytes:
        url = f"{self.base}/storage/v1/object/{BUCKET}/{path}"
        headers = {
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
        }
        r = self._storage_client.get(url, headers=headers)
        if r.status_code >= 400:
            raise RuntimeError(
                f"storage download failed: {r.status_code} {r.text[:300]}"
            )
        return r.content

    def storage_delete(self, path: str) -> None:
        url = f"{self.base}/storage/v1/object/{BUCKET}"
        headers = {
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
        }
        r = self._storage_client.request(
            "DELETE", url, json={"prefixes": [path]}, headers=headers
        )
        if r.status_code >= 400:
            log.warning(
                "storage delete %s: %s %s", path, r.status_code, r.text[:200]
            )


@lru_cache(maxsize=1)
def get_supabase() -> SupabaseREST:
    if not settings.supabase_enabled:
        raise RuntimeError("Supabase is not configured")
    return SupabaseREST(settings.supabase_url, settings.supabase_service_role_key)
