import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.core.llm import LLMRefusedError, LLMUnavailableError
from app.main import create_app
from tests.conftest import FakeLLM, make_settings, make_suggestion

SCENARIO_MESSAGE = "А монтаж можно оплатить мастеру картой?"


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["llm_mode"] == "mock"
    assert len(body["kb_version"]) == 12
    assert response.headers["x-request-id"]


def test_suggest_returns_both_blocks_and_saves(client, settings):
    response = client.post("/api/v1/suggest", json={"message": SCENARIO_MESSAGE, "lead": {"id": 77}})
    assert response.status_code == 200
    body = response.json()
    assert body["suggestion"]["client_reply"]
    assert "upsell" in body["suggestion"]
    suggestion_id = body["meta"]["suggestion_id"]

    stored = client.get(f"/api/v1/suggestions/{suggestion_id}")
    assert stored.status_code == 200
    assert stored.json()["lead_id"] == 77
    assert stored.json()["suggestion"] == body["suggestion"]

    with sqlite3.connect(settings.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM suggestions").fetchone()[0] == 1


def test_saved_request_is_masked(client):
    body = client.post("/api/v1/suggest", json={"message": "Звоните 89161234567"}).json()
    stored = client.get(f"/api/v1/suggestions/{body['meta']['suggestion_id']}").json()
    assert stored["request"]["message"] == "Звоните [PHONE]"


def test_unknown_suggestion_404(client):
    response = client.get("/api/v1/suggestions/nope")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"message": "   "},
        {"message": "ok", "history": [{"role": "boss", "text": "x"}]},
        {"message": "ok", "lead": {"budget": -5}},
    ],
)
def test_validation_errors_have_unified_format(client, payload):
    response = client.post("/api/v1/suggest", json=payload)
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "invalid_request"
    assert error["details"]


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (LLMUnavailableError("нет сети"), 503, "llm_unavailable"),
        (LLMRefusedError("отказ"), 502, "llm_refused"),
    ],
)
def test_llm_errors_are_mapped(tmp_path, error, status, code):
    with TestClient(create_app(make_settings(tmp_path), llm=FakeLLM(error))) as client:
        response = client.post("/api/v1/suggest", json={"message": "?"})
    assert response.status_code == status
    assert response.json()["error"]["code"] == code


def test_fake_llm_is_used_when_injected(tmp_path):
    with TestClient(create_app(make_settings(tmp_path), llm=FakeLLM(make_suggestion()))) as client:
        body = client.post("/api/v1/suggest", json={"message": "?"}).json()
    assert body["meta"]["model"] == "fake-model"


def test_api_token(tmp_path):
    with TestClient(create_app(make_settings(tmp_path, api_token="secret"))) as client:
        assert client.post("/api/v1/suggest", json={"message": "?"}).status_code == 401
        ok = client.post("/api/v1/suggest", json={"message": "?"}, headers={"Authorization": "Bearer secret"})
        assert ok.status_code == 200
        assert client.get("/health").status_code == 200  # health без токена


def test_kb_endpoints(client):
    body = client.get("/api/v1/kb").json()
    assert body["counts"]["products"] == 13
    assert body["products"][0]["id"] == "ac-basic-07"
    reloaded = client.post("/api/v1/kb/reload").json()
    assert reloaded == {"version": body["version"], "changed": False, "counts": body["counts"]}


def test_kb_reload_error_keeps_old_version(tmp_path, kb_copy):
    settings = make_settings(tmp_path, kb_dir=kb_copy)
    with TestClient(create_app(settings)) as client:
        version = client.get("/health").json()["kb_version"]
        path = kb_copy / "products.yaml"
        path.write_text(
            path.read_text(encoding="utf-8").replace("price: 32900", "price: abc"), encoding="utf-8"
        )
        response = client.post("/api/v1/kb/reload")
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "kb_invalid"
        assert any("price" in d for d in response.json()["error"]["details"])
        assert client.get("/health").json()["kb_version"] == version


def test_admin_token(tmp_path):
    with TestClient(create_app(make_settings(tmp_path, admin_token="adm"))) as client:
        assert client.get("/api/v1/kb").status_code == 401
        assert client.post("/api/v1/kb/reload").status_code == 401
        assert client.get("/api/v1/kb", headers={"Authorization": "Bearer adm"}).status_code == 200
        assert client.get("/api/v1/kb", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_demo_page_and_scenarios(client):
    page = client.get("/")
    assert page.status_code == 200
    assert "AI-помощник менеджера" in page.text
    assert "LLM: mock" in page.text
    assert client.get("/static/app.js").status_code == 200
    scenarios = client.get("/api/v1/demo/scenarios").json()
    assert [s["id"] for s in scenarios][:2] == ["price-install", "out-of-kb"]


def test_broken_kb_prevents_startup(tmp_path, kb_copy):
    (kb_copy / "faq.yaml").write_text("не список", encoding="utf-8")
    from app.kb.loader import KBValidationError

    with pytest.raises(KBValidationError), TestClient(create_app(make_settings(tmp_path, kb_dir=kb_copy))):
        pass


def test_health_is_degraded_without_llm_credentials(tmp_path):
    class NoKeyLLM(FakeLLM):
        problem = "Не найдены учётные данные Claude API"

    with TestClient(create_app(make_settings(tmp_path), llm=NoKeyLLM(make_suggestion()))) as client:
        body = client.get("/health").json()
    assert body["status"] == "degraded"
    assert "учётные данные" in body["llm_problem"]
