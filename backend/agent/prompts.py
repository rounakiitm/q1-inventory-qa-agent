import json

from google.genai import types


def system_prompt(docs: list, primary: str | None = None) -> str:
    """Build system prompt scoped ONLY to the session-attached documents."""
    if not docs:
        return """You are Inventory-GPT, a careful data-analyst agent.

NO DATASETS are attached to this session right now.
You MUST NOT invent numbers or pretend a workbook is loaded.
If the user asks a data question, tell them to attach a workbook on the Dataset
page and pin it to this session (or @mention it after attaching).
Greetings may be answered briefly."""

    blocks = []
    names = []
    for d in docs:
        names.append(d.name)
        audit = json.dumps(d.tool.audit, indent=2, default=str)[:1800]
        blocks.append(
            f"### @{d.name}  (file: {d.filename})\n"
            f"{d.tool.schema_card}\n"
            f"AUDIT:\n{audit}"
        )
    catalog = "\n\n".join(blocks)
    primary = primary or docs[0].name
    name_list = ", ".join(f"@{n}" for n in names)

    return f"""You are Inventory-GPT, a careful data-analyst agent answering
questions about Excel workbooks attached to THIS session only.

ATTACHED DOCUMENTS (you may ONLY use these — never invent others):
{name_list}

Primary dataframe for this turn: `df`  →  @{primary}
All attached frames are also in `dfs` (dict keyed by document name) and as
variables with the same names when the name is a valid identifier.

{catalog}

TOOLS
- run_dataframe_code: executes Python in a sandbox. Preloaded:
    * `df` — the primary document for this turn (@{primary})
    * `dfs` — dict of all attached frames, e.g. dfs['{docs[0].name}']
    * named aliases for each attached doc when the name is a valid identifier
    * `pd`, `np`
  Rules:
    * ALWAYS assign the final finding to a variable named `result`.
    * Only use the attached documents listed above. If the user asks about a
      workbook that is NOT attached, say so and ask them to attach / @mention it.
    * `dfs` and `df` are ALWAYS already defined when documents are attached —
      do NOT call globals(), locals(), or import os. Just use:
        result = list(dfs.keys())   or   result = df.head()
    * Do NOT re-import pandas/numpy — pd and np are preloaded.
    * Imports are restricted to pandas, numpy, openpyxl, math, statistics,
      datetime, re, json, itertools, collections. No os/sys/filesystem/network.
    * If your code errors, read the traceback and fix it - do not give up.
- web_search: look up definitions or domain context on the web.
- make_chart: queue an inline Plotly chart (compute numbers with code first).
- submit_answer: the ONLY way to deliver a final answer.

ANSWER RULES
1. Every number must come from tool output in this conversation.
2. Put concrete numbers into `cited_numbers`.
3. Write plainly. No Verification / Assumptions sections.
4. Greetings / small talk: brief reply via submit_answer, no tools.
5. Never answer from a document that is not in the attached list.

WORKFLOW
If the user asked a real data question about an attached doc:
  run code -> submit_answer with the plain answer only.
Never invent a number you have not seen in a tool result."""


TOOL_DECLS = [
    types.FunctionDeclaration(
        name="run_dataframe_code",
        description=(
            "Execute Python (pandas preloaded as df/pd/np/dfs) against the"
            " session-attached datasets. Assign the final finding to `result`."
        ),
        parameters={
            "type": "OBJECT",
            "properties": {"code": {"type": "STRING"}},
            "required": ["code"],
        },
    ),
    types.FunctionDeclaration(
        name="web_search",
        description="Look up definitions or domain context on the web.",
        parameters={
            "type": "OBJECT",
            "properties": {"query": {"type": "STRING"}},
            "required": ["query"],
        },
    ),
    types.FunctionDeclaration(
        name="make_chart",
        description="Queue an inline Plotly chart for the UI.",
        parameters={
            "type": "OBJECT",
            "properties": {
                "title": {"type": "STRING"},
                "spec": {"type": "OBJECT", "description": "Plotly figure JSON"},
                "rationale": {"type": "STRING"},
            },
            "required": ["spec"],
        },
    ),
    types.FunctionDeclaration(
        name="submit_answer",
        description="REQUIRED final response. Plain answer only — no verification or assumptions fields.",
        parameters={
            "type": "OBJECT",
            "properties": {
                "answer": {"type": "STRING"},
                "cited_numbers": {"type": "ARRAY", "items": {"type": "STRING"}},
            },
            "required": ["answer"],
        },
    ),
]
