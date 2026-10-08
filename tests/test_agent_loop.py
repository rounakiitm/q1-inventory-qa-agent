"""Agent-loop tests with a scripted fake LLM (no network, no API key)."""

from types import SimpleNamespace

import pandas as pd
from google.genai import types

from backend.agent.core import Agent
from backend.storage.db import DB
from backend.tools.code_runner import CodeRunner


class FakeCandidate:
    def __init__(self, parts):
        self.content = types.Content(role="model", parts=parts)
        self.grounding_metadata = None


class FakeResponse:
    def __init__(self, parts):
        self.candidates = [FakeCandidate(parts)]
        self.usage_metadata = None

    @property
    def text(self):
        for p in self.candidates[0].content.parts:
            if p.text:
                return p.text
        return ""


class FakeLLM:
    def __init__(self, scripted):
        self.scripted = list(scripted)
        self.calls = 0

    def generate(self, contents, config=None):
        self.calls += 1
        return self.scripted.pop(0)

    def search(self, query):
        return "definition text", [{"title": "x", "uri": "http://x"}]


class FakeSearch:
    def search(self, query):
        return {"ok": True, "summary": "def", "sources": [{"title": "x", "uri": "u"}]}


def make_agent(scripted, tmp_path):
    db = DB(tmp_path / "test.db")
    sid = db.create_session()
    df = pd.DataFrame({"Product": ["A", "B"], "Sold": [5, 7]})
    tiny = SimpleNamespace(
        df=df,
        path=tmp_path / "tiny.xlsx",
        fingerprint="x",
        schema_card="- Product (string)\n- Sold (number)",
        audit={"rows": 2, "notes": [], "integrity": None, "suggested_questions": []},
    )
    agent = Agent(FakeLLM(scripted), tiny, CodeRunner(), FakeSearch(), db, sid)
    return agent, db, sid


def fc(name, args=None):
    return types.Part(function_call=types.FunctionCall(name=name, args=args or {}))


def test_tool_then_submit(tmp_path):
    scripted = [
        FakeResponse([fc("run_dataframe_code", {"code": "result = df['Sold'].sum()"})]),
        FakeResponse(
            [
                fc(
                    "submit_answer",
                    {
                        "answer": "Total sold is 12.",
                        "cited_numbers": ["12"],
                    },
                )
            ]
        ),
    ]
    agent, db, sid = make_agent(scripted, tmp_path)
    answer = agent.ask("How many units sold in total?")
    assert "12" in answer.answer
    assert answer.verification is None
    assert answer.assumptions == []
    runs = db.get_tool_runs(sid)
    assert runs[0]["tool"] == "run_dataframe_code"
    assert runs[0]["ok"] is True


def test_self_repair_on_error(tmp_path):
    scripted = [
        FakeResponse([fc("run_dataframe_code", {"code": "result = df['Nope'].sum()"})]),
        FakeResponse([fc("run_dataframe_code", {"code": "result = df['Sold'].sum()"})]),
        FakeResponse([fc("submit_answer", {"answer": "12"})]),
    ]
    agent, db, sid = make_agent(scripted, tmp_path)
    answer = agent.ask("total?")
    assert "12" in answer.answer
    runs = db.get_tool_runs(sid)
    assert runs[0]["ok"] is False and runs[1]["ok"] is True


def test_iteration_cap_honest_failure(tmp_path):
    scripted = [
        FakeResponse([fc("run_dataframe_code", {"code": "result = df['Nope'].sum()"})])
    ] * 20
    agent, _, _ = make_agent(scripted, tmp_path)
    answer = agent.ask("total?")
    assert answer.incomplete is True
    assert answer.incomplete_reason == "iteration_cap"
    assert "Continue" in answer.answer or "reasoning-step" in answer.answer
    assert not answer.verified


def test_chart_and_search(tmp_path):
    scripted = [
        FakeResponse([fc("web_search", {"query": "what is stock"})]),
        FakeResponse([fc("make_chart", {"title": "t", "spec": {"data": []}})]),
        FakeResponse([fc("submit_answer", {"answer": "done", "cited_numbers": []})]),
    ]
    agent, _, _ = make_agent(scripted, tmp_path)
    answer = agent.ask("explain and chart")
    assert answer.charts[0].title == "t"
    assert answer.sources[0].uri == "u"
