"""Живая модель за паролем на стенде в mock-режиме: вход, выбор ассистента, лимиты, страница."""

import pytest
from fastapi.testclient import TestClient

from app.api import live as live_module
from app.core.llm_registry import LLMRegistry
from app.main import create_app
from tests.conftest import FakeLLM, make_settings, make_suggestion

PASSWORD = "correct-horse-battery-staple"
BODY = {"message": "Сколько длится монтаж?"}
LIVE_REPLY = "Стандартный монтаж занимает 3–4 часа. Когда вам удобно?"


def fake(model: str, problem: str | None = None) -> FakeLLM:
    llm = FakeLLM(make_suggestion(client_reply=LIVE_REPLY, kb_refs=["faq-install-duration"]))
    llm.model = model
    llm.problem = problem
    return llm


def live_registry(**problems) -> LLMRegistry:
    clients = {"grok": fake("grok-4.7", problems.get("grok")), "kimi": fake("kimi-k3", problems.get("kimi"))}
    return LLMRegistry(clients, "grok", ["kimi"])


@pytest.fixture
def stand(tmp_path):
    settings = make_settings(tmp_path, live_demo_password=PASSWORD, live_demo_daily_limit=3)
    with TestClient(create_app(settings, live_llm=live_registry())) as client:
        yield client


def test_without_password_answers_come_from_recordings(stand):
    meta = stand.post("/api/v1/suggest", json=BODY).json()["meta"]
    assert meta["llm_mode"] == "mock"


def test_with_password_answers_come_from_live_model(stand):
    headers = {"X-Live-Password": PASSWORD}
    body = stand.post("/api/v1/suggest", json=BODY, headers=headers).json()
    assert body["meta"]["llm_mode"] == "fake" and body["meta"]["provider"] == "grok"
    assert body["suggestion"]["client_reply"] == LIVE_REPLY
    chosen = stand.post("/api/v1/suggest?provider=kimi", json=BODY, headers=headers).json()["meta"]
    assert chosen["provider"] == "kimi" and chosen["model"] == "kimi-k3"


def test_wrong_password_is_rejected(stand):
    response = stand.post("/api/v1/suggest", json=BODY, headers={"X-Live-Password": "guess"})
    assert response.status_code == 401
    assert "пароль" in response.json()["error"]["message"].lower()


def test_login_returns_models(stand):
    data = stand.post("/api/v1/live/login", json={"password": PASSWORD}).json()
    assert data["default"] == "grok"
    assert [p["id"] for p in data["providers"]] == ["grok", "kimi"]
    assert stand.post("/api/v1/live/login", json={"password": "nope"}).status_code == 401


def test_brute_force_is_throttled(stand):
    for _ in range(live_module.MAX_FAILURES):
        assert stand.post("/api/v1/live/login", json={"password": "nope"}).status_code == 401
    # Дальше отказ даже с верным паролем — пока не пройдёт окно.
    assert stand.post("/api/v1/live/login", json={"password": PASSWORD}).status_code == 429


def test_daily_limit(stand):
    headers = {"X-Live-Password": PASSWORD}
    for _ in range(3):
        assert stand.post("/api/v1/suggest", json=BODY, headers=headers).status_code == 200
    limited = stand.post("/api/v1/suggest", json=BODY, headers=headers)
    assert limited.status_code == 429 and "Лимит" in limited.json()["error"]["message"]
    # Без пароля записанные ответы работают как раньше.
    assert stand.post("/api/v1/suggest", json=BODY).status_code == 200


def test_unknown_model_does_not_spend_the_limit(stand):
    headers = {"X-Live-Password": PASSWORD}
    for _ in range(5):
        assert stand.post("/api/v1/suggest?provider=gpt", json=BODY, headers=headers).status_code == 422
    assert stand.post("/api/v1/suggest", json=BODY, headers=headers).status_code == 200


def test_page_and_health(stand):
    page = stand.get("/").text
    assert 'id="live-demo"' in page and "LLM: mock" in page
    assert 'id="live-demo"' not in stand.get("/amocrm").text
    assert stand.get("/health").json()["live_demo"] == "enabled"


@pytest.mark.parametrize(
    ("overrides", "live_llm"),
    [
        ({}, live_registry()),  # пароль не задан
        ({"live_demo_password": PASSWORD, "llm_mode": "live"}, live_registry()),  # и так всё живое
        (
            {"live_demo_password": PASSWORD},
            live_registry(grok="нет шлюза", kimi="нет шлюза"),
        ),  # нечем отвечать
    ],
)
def test_live_demo_is_off(tmp_path, overrides, live_llm):
    llm = fake("grok-4.7") if overrides.get("llm_mode") == "live" else None
    with TestClient(create_app(make_settings(tmp_path, **overrides), llm=llm, live_llm=live_llm)) as client:
        assert 'id="live-demo"' not in client.get("/").text
        assert "live_demo" not in client.get("/health").json()
        assert client.post("/api/v1/live/login", json={"password": PASSWORD}).status_code == 404
        response = client.post("/api/v1/suggest", json=BODY, headers={"X-Live-Password": PASSWORD})
        assert response.status_code == 404
