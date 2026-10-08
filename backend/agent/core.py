"""Agent core: Gemini function-calling loop with self-repair + answer contract.

State kept per answer: tool events stream to `on_event`; sources and charts
accumulate locally; everything persists for reproducibility. Incomplete turns
(stop / iteration cap) save resumable Gemini contents in message meta.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

from google.genai import types

from backend.agent.prompts import TOOL_DECLS, system_prompt
from backend.config import settings
from backend.schemas import AnswerOut, ChartOut, Source

log = logging.getLogger("agent")

SUBMIT = "submit_answer"
NUDGE = (
    "Deliver your final answer now using the submit_answer tool."
    " Plain answer only — no verification or assumptions sections."
)
CONTINUE_NUDGE = (
    "Continue from where you left off on the previous question."
    " Reuse tool results already in this conversation — do not restart from scratch."
    " Finish the analysis and call submit_answer with the final answer."
)


def serialize_contents(contents: list[types.Content]) -> list[dict[str, Any]]:
    """JSON-safe snapshot of a Gemini multi-turn so we can resume later."""
    out: list[dict[str, Any]] = []
    for c in contents:
        parts: list[dict[str, Any]] = []
        for p in c.parts or []:
            if getattr(p, "text", None):
                parts.append({"type": "text", "text": p.text})
            fc = getattr(p, "function_call", None)
            if fc and getattr(fc, "name", None):
                args = dict(fc.args or {})
                parts.append({"type": "function_call", "name": fc.name, "args": args})
            fr = getattr(p, "function_response", None)
            if fr and getattr(fr, "name", None):
                resp = fr.response
                if hasattr(resp, "items"):
                    resp = dict(resp)
                elif not isinstance(resp, dict):
                    resp = {"value": resp}
                parts.append(
                    {"type": "function_response", "name": fr.name, "response": resp or {}}
                )
        out.append({"role": c.role or "user", "parts": parts})
    return out


def deserialize_contents(data: list[dict[str, Any]] | None) -> list[types.Content]:
    if not data:
        return []
    contents: list[types.Content] = []
    for item in data:
        parts: list[types.Part] = []
        for p in item.get("parts") or []:
            kind = p.get("type")
            if kind == "text":
                parts.append(types.Part(text=p.get("text") or ""))
            elif kind == "function_call":
                parts.append(
                    types.Part(
                        function_call=types.FunctionCall(
                            name=p.get("name") or "",
                            args=p.get("args") or {},
                        )
                    )
                )
            elif kind == "function_response":
                parts.append(
                    types.Part(
                        function_response=types.FunctionResponse(
                            name=p.get("name") or "",
                            response=p.get("response") or {},
                        )
                    )
                )
        if parts:
            contents.append(
                types.Content(role=item.get("role") or "user", parts=parts)
            )
    return contents


class Agent:
    def __init__(
        self,
        llm,
        data_tool=None,
        code_runner=None,
        search_tool=None,
        db=None,
        session_id: str = "",
        user_id: str = "",
        on_event=None,
        docs: list | None = None,
        primary: str | None = None,
        cancel_event: threading.Event | None = None,
    ):
        self.llm = llm
        # `docs` is the preferred multi-doc path; single data_tool kept for tests.
        if docs is not None:
            self.docs = list(docs)
        elif data_tool is not None:
            from types import SimpleNamespace

            self.docs = [
                SimpleNamespace(
                    name="df",
                    filename=getattr(data_tool.path, "name", "dataset"),
                    tool=data_tool,
                )
            ]
        else:
            self.docs = []
        self.primary = primary or (self.docs[0].name if self.docs else None)
        self.data_tool = data_tool or (self.docs[0].tool if self.docs else None)
        self.runner = code_runner
        self.search = search_tool
        self.db = db
        self.sid = session_id
        self.user_id = user_id
        self.on_event = on_event or (lambda event, data: None)
        self.cancel_event = cancel_event
        self._config = types.GenerateContentConfig(
            system_instruction=system_prompt(self.docs, self.primary),
            tools=[types.Tool(function_declarations=TOOL_DECLS)],
            temperature=0.1,
        )

    def _frames(self) -> dict:
        return {d.name: d.tool.df for d in self.docs}

    def _cancelled(self) -> bool:
        return bool(self.cancel_event and self.cancel_event.is_set())

    # ------------------------------------------------------------------ main
    def ask(
        self,
        question: str,
        history: list[types.Content] | None = None,
        *,
        resume: bool = False,
    ) -> AnswerOut:
        contents = list(history or [])
        if question:
            contents.append(
                types.Content(role="user", parts=[types.Part(text=question)])
            )

        sources: list[dict] = []
        charts: list[dict] = []
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "llm_calls": 0}
        answer: AnswerOut | None = None
        nudged = False
        stop_reason: str | None = None

        for _step in range(settings.agent_max_iterations):
            if self._cancelled():
                stop_reason = "stopped"
                break

            resp = self.llm.generate(contents, self._config)
            usage_meta = getattr(resp, "usage_metadata", None)
            if usage_meta:
                usage["prompt_tokens"] += usage_meta.prompt_token_count or 0
                usage["completion_tokens"] += usage_meta.candidates_token_count or 0
                usage["llm_calls"] += 1
                self.db.add_usage(
                    self.sid,
                    usage_meta.prompt_token_count or 0,
                    usage_meta.candidates_token_count or 0,
                    user_id=self.user_id,
                )

            if self._cancelled():
                stop_reason = "stopped"
                break

            candidates = getattr(resp, "candidates", None)
            calls: list[types.FunctionCall] = []
            if candidates and candidates[0].content and candidates[0].content.parts:
                calls = [
                    part.function_call
                    for part in candidates[0].content.parts
                    if part and part.function_call
                ]

            if not calls:
                # plain text instead of the contract -> nudge once, then accept
                text = resp.text or ""
                contents.append(
                    types.Content(role="model", parts=[types.Part(text=text)])
                )
                if nudged:
                    answer = self._fallback(text)
                    break
                nudged = True
                contents.append(
                    types.Content(role="user", parts=[types.Part(text=NUDGE)])
                )
                continue

            contents.append(candidates[0].content)  # preserve the model turn
            submitted = False
            for call in calls:
                if self._cancelled():
                    stop_reason = "stopped"
                    break
                if call.name == SUBMIT:
                    candidate = self._contract(call.args or {})
                    if candidate is not None:
                        answer = candidate
                        submitted = True
                        contents.append(self._fn_response(SUBMIT, {"ok": True}))
                    else:
                        contents.append(
                            self._fn_response(
                                SUBMIT,
                                {
                                    "ok": False,
                                    "error": "answer field missing or empty",
                                },
                            )
                        )
                    continue
                result = self._dispatch(call.name, dict(call.args or {}), sources, charts)
                contents.append(self._fn_response(call.name, result))

            if stop_reason == "stopped":
                break
            if submitted:
                break
        else:
            # loop exhausted without break from submit/cancel
            if answer is None and stop_reason is None:
                stop_reason = "iteration_cap"

        if answer is None and stop_reason is None:
            stop_reason = "iteration_cap"

        if answer is None:
            if stop_reason == "stopped":
                text = (
                    "Stopped. Progress so far is saved — press Continue to resume"
                    " from this point."
                )
            else:
                text = (
                    "I hit the reasoning-step limit "
                    f"({settings.agent_max_iterations} steps) before finishing."
                    " Press Continue to keep going from where I left off, or"
                    " rephrase the question."
                )
            answer = AnswerOut(
                session_id=self.sid,
                answer=text,
                incomplete=True,
                incomplete_reason=stop_reason or "iteration_cap",
            )
        else:
            answer.incomplete = False
            answer.incomplete_reason = None

        answer.charts = [ChartOut(**c) for c in charts]
        answer.sources = [Source(**s) for s in sources]
        answer.tool_runs = self.db.get_tool_runs(
            self.sid, limit=8, user_id=self.user_id or None
        )
        answer.usage = usage
        answer.verified = False
        answer.verification = None
        answer.assumptions = []

        meta: dict[str, Any] = {
            "cited_numbers": answer.cited_numbers,
            "charts": [c for c in charts],
            "sources": [s for s in sources],
            "usage": usage,
            "incomplete": answer.incomplete,
            "incomplete_reason": answer.incomplete_reason,
            "resume": resume,
        }
        if answer.incomplete:
            meta["resume_contents"] = serialize_contents(contents)

        self.db.add_message(
            self.sid,
            "assistant",
            answer.answer,
            meta=meta,
            user_id=self.user_id,
        )
        return answer

    # ------------------------------------------------------------------ parts
    @staticmethod
    def _fn_response(name: str, response: dict) -> types.Content:
        return types.Content(
            role="user",
            parts=[
                types.Part(
                    function_response=types.FunctionResponse(
                        name=name, response=response
                    )
                )
            ],
        )

    def _contract(self, args: dict) -> AnswerOut | None:
        text = str(args.get("answer") or "").strip()
        if not text:
            return None
        return AnswerOut(
            session_id=self.sid,
            answer=text,
            cited_numbers=[str(n) for n in (args.get("cited_numbers") or [])],
            verification=None,
            assumptions=[],
        )

    def _dispatch(
        self, name: str, args: dict, sources: list[dict], charts: list[dict]
    ) -> dict:
        self.on_event(
            "tool_call",
            {
                "name": name,
                "args": {
                    k: (str(v)[:1200] if k == "code" else v)
                    for k, v in args.items()
                },
            },
        )
        t0 = time.perf_counter()
        try:
            if name == "run_dataframe_code":
                result = self.runner.run(
                    args.get("code") or "",
                    frames=self._frames(),
                    primary=self.primary,
                )
            elif name == "web_search":
                result = self.search.search(args.get("query") or "")
                for s in result.get("sources") or []:
                    sources.append(
                        s if isinstance(s, dict) else {"title": str(s), "uri": ""}
                    )
            elif name == "make_chart":
                title = str(args.get("title") or "Chart")
                spec = args.get("spec") or {}
                if not isinstance(spec, dict):
                    spec = {}
                charts.append(
                    {
                        "title": title,
                        "spec": spec,
                        "rationale": str(args.get("rationale") or ""),
                    }
                )
                result = {"ok": True, "title": title}
            else:
                result = {"ok": False, "error": f"unknown tool: {name}"}
        except Exception as exc:  # noqa: BLE001
            log.exception("tool %s failed", name)
            result = {"ok": False, "error": str(exc)}

        ok = bool(result.get("ok"))
        latency_ms = round((time.perf_counter() - t0) * 1000, 1)
        summary = (
            result.get("result_repr")
            or result.get("summary")
            or result.get("error")
            or result.get("title")
            or json.dumps(result, default=str)[:800]
        )
        self.on_event(
            "tool_result",
            {
                "name": name,
                "ok": ok,
                "latency_ms": latency_ms,
                "summary": str(summary)[:2000],
            },
        )
        self.db.add_tool_run(
            self.sid,
            name,
            args,
            result,
            ok,
            latency_ms,
            user_id=self.user_id,
        )
        # Compact payload back to the model (keep result_repr for code tools).
        if name == "run_dataframe_code":
            return {
                "ok": ok,
                "error": result.get("error"),
                "result_repr": result.get("result_repr"),
                "result_type": result.get("result_type"),
                "stdout": (result.get("stdout") or "")[:2000],
            }
        if name == "web_search":
            return {
                "ok": ok,
                "summary": result.get("summary"),
                "sources": result.get("sources") or [],
                "error": result.get("error"),
            }
        return result

    def _fallback(self, text: str) -> AnswerOut:
        return AnswerOut(
            session_id=self.sid,
            answer=text.strip()
            or "I couldn't produce a structured answer. Please try again.",
        )
