"""Tool declarations for the Gemini function-calling protocol."""

# Declared as plain dicts matching the Gemini tool schema; wrapped in
# types.Tool(function_declarations=...) by the agent.
TOOL_DECLS = [
    {
        "name": "run_dataframe_code",
        "description": (
            "Execute Python (pandas preloaded as df/pd/np) against the dataset."
            " Assign the final finding to `result`. Filesystem/network blocked."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {"code": {"type": "STRING"}},
            "required": ["code"],
        },
    },
    {
        "name": "web_search",
        "description": "Look up definitions or domain context on the web.",
        "parameters": {
            "type": "OBJECT",
            "properties": {"query": {"type": "STRING"}},
            "required": ["query"],
        },
    },
    {
        "name": "make_chart",
        "description": (
            "Queue an inline Plotly chart for the UI. Compute the numbers with"
            " run_dataframe_code first. Spec is Plotly figure JSON"
            " ({'data': [...], 'layout': {...}})."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "title": {"type": "STRING"},
                "spec": {"type": "OBJECT"},
                "rationale": {"type": "STRING"},
            },
            "required": ["spec"],
        },
    },
    {
        "name": "submit_answer",
        "description": (
            "REQUIRED final response format. Numeric answers must be verified"
            " by an independent recomputation before submission."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "answer": {"type": "STRING"},
                "cited_numbers": {"type": "ARRAY", "items": {"type": "STRING"}},
                "verification": {"type": "STRING"},
                "assumptions": {"type": "ARRAY", "items": {"type": "STRING"}},
            },
            "required": ["answer"],
        },
    },
]
