"""Web-search lookup tool backed by Gemini google_search grounding."""

import logging

log = logging.getLogger("tools.search")


class SearchTool:
    def __init__(self, llm):
        self.llm = llm

    def search(self, query: str) -> dict:
        if not query or not query.strip():
            return {"ok": False, "error": "empty query", "summary": "", "sources": []}
        try:
            text, sources = self.llm.search(query.strip())
            return {
                "ok": True,
                "query": query.strip(),
                "summary": text,
                "sources": sources,
            }
        except Exception as exc:  # noqa: BLE001 - surfaced to the agent
            log.warning("search failed: %s", exc)
            return {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "summary": "",
                "sources": [],
            }
