from typing import Any

from pydantic import BaseModel, Field


class SessionInfo(BaseModel):
    session_id: str


class AuthCredentials(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=6, max_length=200)


class ChatMessageIn(BaseModel):
    question: str = Field(default="", max_length=4000)
    model: str | None = Field(
        default=None,
        description="Optional low-cost model override (must be in selectable allowlist).",
    )
    # Optional explicit focus docs for this turn (otherwise parsed from @mentions
    # + session attachments).
    doc_ids: list[str] | None = None
    # Resume an incomplete turn (stopped / hit reasoning limit) using saved tool state.
    continue_from_incomplete: bool = False


class SessionDocsIn(BaseModel):
    doc_ids: list[str] = []


class DocAttachIn(BaseModel):
    doc_id: str


class SessionSummary(BaseModel):
    session_id: str
    created_at: str
    last_activity: str
    n_messages: int = 0
    preview: str = "New session"


class Source(BaseModel):
    title: str = ""
    uri: str = ""


class ChartOut(BaseModel):
    title: str = "Chart"
    spec: dict[str, Any] = {}
    rationale: str = ""


class AnswerOut(BaseModel):
    session_id: str
    answer: str
    cited_numbers: list[str] = []
    verification: str | None = None
    assumptions: list[str] = []
    sources: list[Source] = []
    charts: list[ChartOut] = []
    tool_runs: list[dict[str, Any]] = []
    usage: dict[str, int] = {}
    verified: bool = False
    incomplete: bool = False
    incomplete_reason: str | None = None  # "stopped" | "iteration_cap"


class DatasetInfo(BaseModel):
    ok: bool = True
    file: str = ""
    shape: list[int] = []
    schema_card: str = ""
    audit: dict[str, Any] = {}
