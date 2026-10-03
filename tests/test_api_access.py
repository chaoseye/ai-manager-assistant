"""HTTP API: защита живой модели, токен на демо-странице, заголовки, крайние входы.

xfail(strict=True) — известный недочёт (см. шапку test_money_pii_props.py).
"""

import re

import pytest
from fastapi.testclient import TestClient

from app.api import live as live_module
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


def guesses(client, count, forwarded):
    return [
        client.post(
            "/api/v1/live/login", json={"password": f"guess{i}"}, headers={"X-Forwarded-For": forwarded(i)}
        ).status_code
        for i in range(count)
    ]


def test_lockout_cannot_be_bypassed_with_forwarded_for(stand):
    # Без доверенного прокси X-Forwarded-For не учитывается: адрес — из соединения.
    statuses = guesses(stand, 30, lambda i: f"10.0.0.{i}")
    assert statuses[:5] == [401] * 5 and set(statuses[5:]) == {429}


@pytest.fixture
def proxied_stand(tmp_path):
    """Стенд за прокси, который дописывает адрес клиента справа (Vercel, nginx)."""
    live = LLMRegistry({"grok": FakeLLM(make_suggestion())}, "grok")
    settings = make_settings(tmp_path, live_demo_password=PASSWORD, trust_forwarded_for=True)
    with TestClient(create_app(settings, live_llm=live)) as client:
        yield client


def test_behind_proxy_the_rightmost_address_counts(proxied_stand):
    # Клиент пишет в заголовок что угодно, но правую запись добавил прокси — по ней и блокировка.
    statuses = guesses(proxied_stand, 8, lambda i: f"10.0.0.{i}, 203.0.113.7")
    assert statuses[:5] == [401] * 5 and set(statuses[5:]) == {429}
    other = proxied_stand.post(
        "/api/v1/live/login", json={"password": PASSWORD}, headers={"X-Forwarded-For": "198.51.100.1"}
    )
    assert other.status_code == 200  # другой клиент за тем же прокси не заблокирован


def test_guessing_from_many_addresses_hits_the_total_limit(proxied_stand):
    total = live_module.MAX_FAILURES_TOTAL
    statuses = guesses(proxied_stand, total + 3, lambda i: f"203.0.113.{i}")
    assert statuses[:total] == [401] * total and set(statuses[total:]) == {429}


def test_failures_of_old_addresses_are_forgotten(proxied_stand, monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr(live_module.time, "monotonic", lambda: clock["now"])
    guesses(proxied_stand, 40, lambda i: f"203.0.113.{i}")
    clock["now"] += live_module.FAILURE_WINDOW_SECONDS + 1
    guesses(proxied_stand, 20, lambda i: f"198.51.100.{i}")
    live = proxied_stand.app.state.live_demo
    assert len(live._failures) <= live_module.MAX_FAILURES_TOTAL
    assert all(address.startswith("198.51.100.") for address in live._failures)


def test_demo_page_can_send_api_token(tmp_path):
    with TestClient(create_app(make_settings(tmp_path, api_token="t0ken"))) as client:
        html = client.get("/").text
        js = client.get("/static/app.js").text
        unauthorized = client.post("/api/v1/suggest", json=BODY)
        authorized = client.post("/api/v1/suggest", json=BODY, headers={"Authorization": "Bearer t0ken"})
    # Страница сразу спрашивает токен и отправляет его, а 401 говорит, что нужен именно токен.
    assert 'id="api-token-form"' in html and 'data-required="1"' in html
    assert "Authorization" in js
    assert unauthorized.status_code == 401 and unauthorized.headers.get("www-authenticate") == "Bearer"
    assert authorized.status_code == 200


def test_open_service_does_not_ask_for_token(client):
    assert 'data-required=""' in client.get("/").text


def test_wrong_live_password_is_not_a_token_error(stand):
    response = stand.post("/api/v1/suggest", json=BODY, headers={"X-Live-Password": "guess"})
    assert response.status_code == 401 and "www-authenticate" not in response.headers


def test_error_handler_keeps_exception_headers(client):
    # До исправления обработчик ошибок терял заголовки: у 405 пропадал Allow.
    assert "POST" in client.get("/api/v1/suggest").headers.get("allow", "")


@pytest.mark.parametrize("path", ["/", "/amocrm"])
def test_demo_pages_have_strict_csp(client, path):
    headers = client.get(path).headers
    csp = headers.get("content-security-policy", "")
    assert "default-src 'self'" in csp and "frame-ancestors 'none'" in csp and "'unsafe-inline'" not in csp
    assert headers.get("x-frame-options") == "DENY"
    assert headers.get("x-content-type-options") == "nosniff"
    assert headers.get("referrer-policy") == "same-origin"


@pytest.mark.parametrize("path", ["/", "/amocrm"])
def test_demo_pages_load_nothing_inline_or_foreign(client, path):
    # Строгий CSP не ломает страницы, только пока в них нет встроенных скриптов, стилей и чужих адресов.
    html = client.get(path).text
    assert "<style" not in html and "style=" not in html
    scripts = re.findall(r"<script([^>]*)>(.*?)</script>", html, re.S)
    assert scripts, "на странице должны быть свои скрипты"
    for attributes, body in scripts:
        assert re.search(r'src="http://testserver/static/[\w.]+\?v=\w+"', attributes) and not body.strip()
    absolute = re.findall(r'(?:href|src)="(https?://[^"]+)"', html)
    assert all(url.startswith("http://testserver/") for url in absolute), absolute


def test_api_docs_keep_their_scripts_but_cannot_be_framed(client):
    headers = client.get("/docs").headers
    assert headers.get("content-security-policy") == "frame-ancestors 'none'"  # Swagger грузит скрипты с CDN


def test_json_responses_are_not_sniffed(client):
    headers = client.get("/health").headers
    assert headers.get("x-content-type-options") == "nosniff"
    assert "content-security-policy" not in headers


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
