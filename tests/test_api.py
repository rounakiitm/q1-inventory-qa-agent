"""API tests with a stubbed agent (no LLM)."""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from backend import main
    from backend.auth.deps import LOCAL_USER_ID

    monkeypatch.setattr(main.settings, "db_path", tmp_path / "api.db")
    monkeypatch.setattr(main.settings, "auth_disabled", True)
    monkeypatch.setattr(main.settings, "supabase_url", "")
    monkeypatch.setattr(main.settings, "supabase_service_role_key", "")
    fresh = main.DB(main.settings.db_path)
    monkeypatch.setattr(main, "db", fresh)
    main.store.clear()
    main.store.bind_db(fresh)
    main.load_default_dataset()
    assert main.store.list(LOCAL_USER_ID), "default dataset should seed"

    class StubAgent:
        def __init__(self, session_id, on_event=None, **kwargs):
            self.sid = session_id

        def ask(self, question, history=None):
            from backend.schemas import AnswerOut

            main.db.add_message(
                self.sid,
                "assistant",
                f"stub answer to: {question}",
                meta={},
            )
            return AnswerOut(
                session_id=self.sid,
                answer=f"stub answer to: {question}",
                verified=True,
                verification="stub check",
            )

    monkeypatch.setattr(
        main,
        "build_agent",
        lambda sid, user_id=None, on_event=None, **kwargs: StubAgent(sid, on_event),
    )
    return TestClient(main.app)


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["auth"] == "local"


def test_auth_config(client):
    resp = client.get("/auth/config")
    assert resp.status_code == 200
    assert resp.json()["auth_required"] is False


def test_session_flow(client):
    sid = client.post("/sessions").json()["session_id"]
    resp = client.post(
        f"/sessions/{sid}/messages", params={"stream": "false"}, json={"question": "hi"}
    )
    assert resp.status_code == 200
    assert resp.json()["answer"].startswith("stub answer")
    history = client.get(f"/sessions/{sid}/messages").json()
    roles = [m["role"] for m in history]
    assert roles == ["user", "assistant"]


def test_missing_session_404(client):
    resp = client.post("/sessions/doesnotexist/messages", json={"question": "hi"})
    assert resp.status_code == 404


def test_dataset_endpoint(client):
    info = client.get("/dataset").json()
    assert info["shape"][0] == 46
    assert "integrity" in info["audit"]
