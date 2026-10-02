"""HTTP API: защита живой модели, токен на демо-странице, заголовки, крайние входы.

xfail(strict=True) — известный недочёт (см. шапку test_money_pii_props.py).
"""

import pytest
from fastapi.testclient import TestClient

from app.core.llm_registry import LLMRegistry
from app.main import create_app
from tests.conftest import FakeLLM, make_settings, make_suggestion

PASSWORD = "correct-horse-battery-staple"
BODY = {"message": "Сколько длится монтаж?"}


@pytest.fixture
def stand(tmp_path):
    live = LLMRegistry({"grok": FakeLLM(make_suggestion())}, "grok")
    settings = make_settings(tmp_path, live_demo_password=PASSWORD, live_demo_daily_limit=1000)
    with TestClient(create_app(settings, live_llm=live)) as client:
        yield client


def test_lockout_for_one_address(stand):
    for _ in range(5):
        assert stand.post("/api/v1/live/login", json={"password": "x"}).status_code == 401
    assert stand.post("/api/v1/live/login", json={"password": PASSWORD}).status_code == 429


@pytest.mark.xfail(
    strict=True, reason="адрес берётся из X-Forwarded-For клиента — блокировку подбора можно обойти"
)
def test_lockout_cannot_be_bypassed_with_forwarded_for(stand):
    statuses = [
        stand.post(
            "/api/v1/live/login", json={"password": f"guess{i}"}, headers={"X-Forwarded-For": f"10.0.0.{i}"}
        ).status_code
        for i in range(30)
    ]
    assert 429 in statuses


@pytest.mark.xfail(strict=True, reason="при API_TOKEN демо-страница не может вызвать /api/v1/suggest")
def test_demo_page_can_send_api_token(tmp_path):
    with TestClient(create_app(make_settings(tmp_path, api_token="t0ken"))) as client:
        assert client.get("/").status_code == 200
        js = client.get("/static/app.js").text
        unauthorized = client.post("/api/v1/suggest", json=BODY)
    # Страница должна уметь отправить токен, а ответ 401 — сказать, что нужен именно он.
    assert "Authorization" in js
    assert unauthorized.headers.get("www-authenticate") == "Bearer"


@pytest.mark.xfail(
    strict=True, reason="у HTML-страниц нет защитных заголовков (CSP, nosniff, frame-ancestors)"
)
def test_html_pages_have_security_headers(client):
    headers = client.get("/").headers
    assert headers.get("x-content-type-options") == "nosniff"
    assert "frame-ancestors" in headers.get("content-security-policy", "")


@pytest.mark.xfail(strict=True, reason="429 приходит с кодом http_error")
def test_429_has_specific_error_code(stand):
    for _ in range(6):
        response = stand.post("/api/v1/live/login", json={"password": "x"})
    assert response.status_code == 429
    assert response.json()["error"]["code"] != "http_error"


@pytest.mark.xfail(strict=True, reason="сообщение из невидимых символов считается непустым и уходит в модель")
def test_invisible_message_is_rejected(client):
    assert client.post("/api/v1/suggest", json={"message": "​​⁠"}).status_code == 422


def test_limits_are_enforced(client):
    payloads = [
        {"message": "а" * 10_001},
        {"message": "x", "history": [{"role": "client", "text": "x"}] * 501},
        {"message": "x", "history": [{"role": "admin", "text": "x"}]},
        {"message": "x", "lead": {"budget": -1}},
    ]
    assert {client.post("/api/v1/suggest", json=p).status_code for p in payloads} == {422}


def test_unknown_and_empty_provider(client):
    assert client.post("/api/v1/suggest?provider=gpt", json=BODY).status_code == 422
    assert client.post("/api/v1/suggest?provider=", json=BODY).status_code == 422


def test_markup_comes_back_as_data(client):
    payload = "<img src=x onerror=alert(1)>"
    response = client.post("/api/v1/suggest", json={"message": payload, "lead": {"contact_name": payload}})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
