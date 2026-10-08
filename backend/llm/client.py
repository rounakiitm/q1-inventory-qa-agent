"""Gemini client wrapper: key-pool rotation + retries + token accounting.

Strategy:
  - 429 (quota)  -> rotate to the next key in the pool immediately
  - 503 (demand) -> rotate with a short cooldown
  - after every key has been tried `max_rounds` times, give up
  - non-retryable errors raise through
"""

import logging
import random
import time

from google import genai
from google.genai import types

from backend.config import settings

log = logging.getLogger("llm")


class PoolExhaustedError(RuntimeError):
    """All keys in the pool failed for one model."""

    def __init__(self, last: str) -> None:
        super().__init__(f"LLM failed after exhausting key pool: {last[:200]}")


class LLMClient:
    def __init__(self, api_keys: list[str] | None = None, model: str | None = None):
        self.model = model or settings.model_name
        self.fallbacks = [m for m in settings.model_ladder if m != self.model]
        self._keys = api_keys or settings.api_keys
        if not self._keys:
            raise ValueError("no Gemini API keys configured (set GEMINI_API_KEYS)")
        self._clients = [genai.Client(api_key=k) for k in self._keys]
        self._idx = random.randrange(len(self._clients))
        self.last_usage: dict[str, int] = {}

    def for_model(self, model: str) -> "LLMClient":
        """Same key pool, different primary model (cheap UI switch)."""
        other = LLMClient.__new__(LLMClient)
        other.model = model
        # Prefer other selectable models as fallbacks (still below 3.5).
        other.fallbacks = [m for m in settings.selectable_models if m != model]
        other._keys = self._keys
        other._clients = self._clients
        other._idx = self._idx
        other.last_usage = {}
        return other

    # ------------------------------------------------------------------ core
    def _attempt(self, call):  # noqa: ANN001, ANN201
        max_rounds = 2
        cooldown = {"429": 0.5, "503": 2.0}
        attempts = len(self._clients) * max_rounds
        last = "unknown error"
        for attempt in range(attempts):
            client = self._clients[self._idx % len(self._clients)]
            key_tag = self._keys[self._idx % len(self._keys)][:8]
            try:
                return call(client)
            except Exception as exc:
                last = str(exc)
                code = "429" if "429" in last or "RESOURCE_EXHAUSTED" in last else (
                    "503" if "503" in last or "UNAVAILABLE" in last else (
                        "403" if "403" in last or "PERMISSION_DENIED" in last else None
                    )
                )
                if code is None:
                    raise
                wait = cooldown.get(code, 0.5) * (1 + random.random() * 0.3)
                log.warning(
                    "llm %s on key %s (attempt %d/%d), rotating: %s",
                    code,
                    key_tag,
                    attempt + 1,
                    attempts,
                    last[:100],
                )
                time.sleep(wait)
                self._idx = (self._idx + 1) % len(self._clients)
        raise PoolExhaustedError(last)

    def _with_fallbacks(self, run):  # noqa: ANN001, ANN202
        """Run against the primary model, then walk the fallback ladder.

        Each model has its own per-key daily quota bucket, so a model switch
        unlocks fresh quota on the same keys.
        """
        try:
            return run(self.model)
        except PoolExhaustedError:
            for fb in self.fallbacks:
                log.warning("model %s exhausted/unavailable, falling back to %s", self.model, fb)
                try:
                    return run(fb)
                except PoolExhaustedError:
                    continue
            raise

    def _account(self, resp) -> dict:  # noqa: ANN001
        usage = getattr(resp, "usage_metadata", None)
        if not usage:
            self.last_usage = {}
            return self.last_usage
        self.last_usage = {
            "prompt_tokens": usage.prompt_token_count or 0,
            "completion_tokens": usage.candidates_token_count or 0,
            "total_tokens": usage.total_token_count or 0,
        }
        return self.last_usage

    def generate(self, contents, config=None):  # noqa: ANN001, ANN201
        def run(model):  # noqa: ANN001
            def call(client):  # noqa: ANN001
                return client.models.generate_content(
                    model=model, contents=contents, config=config
                )

            return self._attempt(call)

        resp = self._with_fallbacks(run)
        self._account(resp)
        return resp

    def search(self, query: str) -> tuple[str, list[dict]]:
        """Grounded lookup. Returns (summary_text, sources)."""
        cfg = types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())],
            temperature=0.1,
        )

        def run(model):  # noqa: ANN001
            def call(client):  # noqa: ANN001
                return client.models.generate_content(
                    model=model, contents=query, config=cfg
                )

            return self._attempt(call)

        resp = self._with_fallbacks(run)
        self._account(resp)
        text = resp.text or ""
        sources: list[dict] = []
        candidates = getattr(resp, "candidates", None)
        if candidates:
            meta = getattr(candidates[0], "grounding_metadata", None)
            for chunk in getattr(meta, "grounding_chunks", None) or []:
                web = getattr(chunk, "web", None)
                if web and (web.uri or web.title):
                    sources.append({"title": web.title or "", "uri": web.uri or ""})
        return text, sources
