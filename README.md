# Inventory Q&A Agent (Q1)

**Prod demo URL:** https://inventory-qa-agent-yp4xrztlaq-el.a.run.app/app/

Ask questions about Excel inventory workbooks in plain English. The agent writes
and runs **pandas** code in a sandbox, can look up definitions with web search,
builds optional charts, and streams its tool trace into a web UI.

## Prod demo URLs

| | URL |
|---|---|
| **Prod demo (UI)** | https://inventory-qa-agent-yp4xrztlaq-el.a.run.app/app/ |
| **API base** | https://inventory-qa-agent-yp4xrztlaq-el.a.run.app |
| **Health** | https://inventory-qa-agent-yp4xrztlaq-el.a.run.app/health |
| **OpenAPI docs** | https://inventory-qa-agent-yp4xrztlaq-el.a.run.app/docs |
| **Source** | https://github.com/rounakiitm/q1-inventory-qa-agent |

Hosted on **Google Cloud Run** (`rounak-iitm`, `asia-south1`).

## Authentication

**Production uses Supabase Auth** (email + password) — not a hand-rolled login.

| Mode | Behaviour |
|---|---|
| **Prod (Cloud Run)** | SPA shows a sign-in / sign-up gate. Credentials go through FastAPI → Supabase Auth (`/auth/v1`). The browser keeps the access token and sends `Authorization: Bearer <jwt>` on every API call. Backend validates the JWT (HS256 with `SUPABASE_JWT_SECRET`, or `/auth/v1/user` fallback) and scopes sessions / workbooks by `user_id`. Workbook files live in Supabase Storage; rows in Supabase Postgres. |
| **Local without Supabase env** | Auth is skipped. A fixed anonymous user is used; data goes to SQLite (`data/agent.db`). |
| **`AUTH_DISABLED=true`** | Forces the anonymous / SQLite path even if Supabase keys are present (useful for tests). |

Same Supabase project is shared with Q3 so one account works on both apps.

Upload an `.xlsx`, open a chat session, ask analytical questions
(e.g. “top SKUs by quantity”, “which warehouses are low on stock”).

## What it does

1. **Upload Excel** → workbook stored (Supabase Storage in prod, local disk in SQLite mode).
2. **Ask a question** → Gemini agent loop with tools:
   - `run_dataframe_code` — generate & execute pandas in a restricted subprocess
   - `web_search` — ground domain terms when needed
   - `make_chart` — Plotly chart from computed data
   - `submit_answer` — final structured reply
3. **SSE stream** → UI shows each tool call/result, then the answer.
4. Optional **notebook export** of the session’s code cells.

```
Browser SPA (/app)
    │  JWT (Supabase Auth) + SSE
    ▼
FastAPI (Cloud Run)
    ├── Gemini function-calling agent
    │     ├── sandbox (pandas / openpyxl)
    │     ├── web search
    │     └── Plotly charts
    └── Persistence
          · Prod: Supabase Auth + Postgres + Storage
          · Local: SQLite + anonymous user
```

## Tech stack

| Layer | Choice | Why |
|---|---|---|
| Frontend | Vanilla HTML / CSS / JS SPA | Fast, no build step; served by FastAPI at `/app` |
| API | **FastAPI** + **Uvicorn** | Async API + SSE chat streaming |
| LLM | **Google Gemini** (`google-genai`) | Function-calling agent loop |
| Data | **pandas** + **openpyxl** | Excel load + analysis |
| Charts | **Plotly** | Interactive charts in answers |
| Auth | **Supabase Auth** + **PyJWT** | Email/password; Bearer JWT on API |
| DB (prod) | **Supabase Postgres** + Storage | Sessions, messages, workbook blobs |
| DB (local) | **SQLite** | Zero-config offline mode |
| Hosting | **Google Cloud Run** (`rounak-iitm`, `asia-south1`) | Containerized deploy |
| Config | **pydantic-settings** + `.env` | Typed env config |
| Tests | **pytest** | Agent / API / sandbox unit tests |

## Run locally

```bash
pip install -r requirements.txt
cp .env.example .env        # set GEMINI_API_KEY (or GEMINI_API_KEYS)
uvicorn backend.main:app --port 8000
# UI: http://localhost:8000/app
```

Without Supabase env vars, auth is skipped and data goes to `data/agent.db`.

### Production auth (Supabase)

1. Create a Supabase project and run `supabase/schema.sql`.
2. Put `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY`,
   `SUPABASE_JWT_SECRET`, and Gemini keys in `.env`.
3. `docker compose up --build` or deploy with `scripts/deploy_cloudrun.sh`.

## Main API

```
GET  /health
GET  /auth/config
GET  /auth/me
GET  /documents                          # workbook library
POST /documents                          # upload .xlsx
POST /sessions
PUT  /sessions/{id}/documents            # attach workbooks
POST /sessions/{id}/messages?stream=true # SSE tool/answer stream
GET  /sessions/{id}/notebook             # export .ipynb
```

## Design notes

- **Dual-mode storage** — same FastAPI surface; `get_db()` picks Supabase or SQLite.
- **Service role + `user_id` filter** — backend scopes all rows by authenticated user.
- **Sandbox** — AST allowlist, timeout, and output caps (trusted-demo boundary, not a full container jail).

## Tests

```bash
pytest -q
```
