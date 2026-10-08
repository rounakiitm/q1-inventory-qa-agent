"""Streamlit thin client: chat UI over the FastAPI backend (SSE)."""

import json
import os

import plotly.graph_objects as go
import requests
import streamlit as st

BASE = os.environ.get("AGENT_URL", "http://localhost:8000")

st.set_page_config(page_title="Inventory Q&A Agent", page_icon="📊", layout="wide")


def api(method: str, path: str, **kwargs):
    return requests.request(method, f"{BASE}{path}", timeout=180, **kwargs)


def ensure_session() -> str:
    if "sid" not in st.session_state:
        st.session_state.sid = api("POST", "/sessions").json()["session_id"]
    return st.session_state.sid


def parse_sse(response):
    event = "message"
    for line in response.iter_lines(decode_unicode=True):
        if not line:
            continue
        if line.startswith("event: "):
            event = line[len("event: ") :]
        elif line.startswith("data: "):
            yield event, json.loads(line[len("data: ") :])


def render_assistant(msg: dict):
    with st.chat_message("assistant"):
        if msg.get("verification"):
            st.success(f"✓ Verified — {msg['verification']}")
        st.markdown(msg["content"])
        for chart in msg.get("charts", []) or []:
            try:
                st.plotly_chart(go.Figure(chart["spec"]), use_container_width=True)
                if chart.get("rationale"):
                    st.caption(f"Chart rationale: {chart['rationale']}")
            except Exception as exc:  # noqa: BLE001
                st.warning(f"chart render failed: {exc}")
        if msg.get("sources"):
            with st.expander("🌐 Sources"):
                for s in msg["sources"]:
                    st.markdown(f"- [{s.get('title') or s.get('uri')}]({s.get('uri')})")
        if msg.get("cited_numbers"):
            st.caption("Numbers cited: " + ", ".join(msg["cited_numbers"]))


# ---------------------------------------------------------------- sidebar
with st.sidebar:
    st.title("📊 Inventory Q&A Agent")
    st.caption("Gemini + pandas sandbox + web grounding")

    uploaded = st.file_uploader("Swap dataset (.xlsx)", type=["xlsx", "xls"])
    if uploaded and st.button("Load dataset"):
        with st.spinner("Loading + auditing..."):
            resp = api("POST", "/dataset", files={"file": uploaded.getvalue()})
        if resp.ok:
            st.session_state.pop("dataset_info", None)
            st.rerun()
        else:
            st.error(resp.json().get("detail", "upload failed"))

    if "dataset_info" not in st.session_state:
        try:
            st.session_state.dataset_info = api("GET", "/dataset").json()
        except Exception:  # noqa: BLE001
            st.session_state.dataset_info = None
    info = st.session_state.get("dataset_info")
    if info:
        st.success(f"Dataset: {info['file']} ({info['shape'][0]} rows)")
        with st.expander("🧾 Schema card"):
            st.code(info["schema_card"])
        with st.expander("🔍 Data audit"):
            audit = info["audit"]
            st.markdown(
                f"Rows **{audit['rows']}** · Cols **{audit['columns']}** · "
                f"Null cells **{audit['null_cells']}** · Duplicates **{audit['duplicate_rows']}**"
            )
            for note in audit.get("notes", []):
                st.markdown(f"- {note}")
            if audit.get("integrity") and audit["integrity"].get("violations"):
                st.warning(
                    f"{audit['integrity']['violations']} rows violate:"
                    f" {audit['integrity']['rule']}"
                )
            for q in audit.get("suggested_questions", [])[:4]:
                st.markdown(f"- _{q}_")

    usage = st.session_state.get("usage", {"prompt_tokens": 0, "completion_tokens": 0, "llm_calls": 0})
    st.divider()
    st.markdown("### Session cost")
    c1, c2 = st.columns(2)
    c1.metric("Prompt tok", f"{usage['prompt_tokens']:,}")
    c2.metric("Completion tok", f"{usage['completion_tokens']:,}")
    st.metric("LLM calls", usage["llm_calls"])

    if st.session_state.get("messages"):
        nb = api("GET", f"/sessions/{st.session_state.sid}/notebook")
        if nb.ok:
            st.download_button(
                "⬇️ Export conversation as notebook",
                nb.content,
                file_name=f"session_{st.session_state.sid}.ipynb",
            )

# ---------------------------------------------------------------- chat state
if "messages" not in st.session_state:
    st.session_state.messages = []

for msg in st.session_state.messages:
    if msg["role"] == "user":
        with st.chat_message("user"):
            st.markdown(msg["content"])
    else:
        render_assistant(msg)

sid = ensure_session()

if question := st.chat_input("Ask about the inventory data..."):
    st.session_state.messages.append({"role": "user", "content": question})
    with st.chat_message("user"):
        st.markdown(question)

    with st.chat_message("assistant"):
        trace_box = st.container()
        answer_holder = st.empty()
        trace_items = []

        with requests.post(
            f"{BASE}/sessions/{sid}/messages",
            params={"stream": "true"},
            json={"question": question},
            stream=True,
        ) as resp:
            if resp.status_code != 200:
                st.error(resp.json().get("detail", "backend error"))
                st.stop()
            answer = None
            for event, data in parse_sse(resp):
                if event == "tool_call":
                    trace_items.append(("call", data))
                    with trace_box:
                        icon = {"run_dataframe_code": "🛠", "web_search": "🌐", "make_chart": "📈"}.get(
                            data["name"], "🔧"
                        )
                        st.markdown(f"{icon} **{data['name']}**")
                        if data["args"].get("code"):
                            st.code(data["args"]["code"], language="python")
                        elif data["args"].get("query"):
                            st.caption(f"query: {data['args']['query']}")
                elif event == "tool_result":
                    trace_items.append(("result", data))
                    with trace_box:
                        status = "✅" if data["ok"] else "❌"
                        st.caption(f"{status} {data['summary'][:220]} ({data['latency_ms']} ms)")
                elif event == "answer":
                    answer = data
                elif event == "error":
                    st.error(f"agent error: {data.get('message')}")
            if answer:
                usage = st.session_state.get("usage", {"prompt_tokens": 0, "completion_tokens": 0, "llm_calls": 0})
                for key in ("prompt_tokens", "completion_tokens", "llm_calls"):
                    usage[key] = usage.get(key, 0) + answer.get("usage", {}).get(key, 0)
                st.session_state.usage = usage

                msg = {
                    "role": "assistant",
                    "content": answer["answer"],
                    "verification": answer.get("verification"),
                    "cited_numbers": answer.get("cited_numbers", []),
                    "charts": answer.get("charts", []),
                    "sources": answer.get("sources", []),
                }
                st.session_state.messages.append(msg)
                render_assistant(msg)
