"""Имитатор amoCRM и регистрация вебхука.

Имитатор гоняется через TestClient: настоящие HTTP-запросы к приложению в mock-режиме, фоновый обработчик,
поддельный amoCRM и mock-LLM с записанными ответами — полный путь без сети и ключей.
"""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.amocrm import setup_webhook, simulate
from app.amocrm.client import AmoRequestError
from app.amocrm.fake import FakeAmoApi
from app.amocrm.setup_webhook import check_destination, register
from app.config import BASE_DIR
from app.main import create_app
from tests.conftest import FakeLLM, make_settings, make_suggestion

SECRET = "sim-secret"
SEED = BASE_DIR / "examples" / "amocrm" / "mock_account.json"


@pytest.fixture
def service(tmp_path, monkeypatch):
    for key, value in {"WEBHOOK_SECRET": SECRET, "LLM_MODE": "mock"}.items():
        monkeypatch.setenv(key, value)
    simulate.get_settings.cache_clear()
    settings = make_settings(
        tmp_path,
        amocrm_mode="mock",
        webhook_secret=SECRET,
        debounce_seconds=0,
        worker_poll_seconds=0.02,
    )
    with TestClient(create_app(settings)) as client:
        yield client
    simulate.get_settings.cache_clear()


def run(client, *argv) -> int:
    return simulate.main([*argv, "--url", "http://testserver", "--wait", "10", "--poll", "0.1"], http=client)


def test_scenario_end_to_end(service, capsys):
    assert run(service, "--scenario", "price-install") == 0
    out = capsys.readouterr().out
    assert "→ Клиент: Добрый день, нужен кондиционер в спальню" in out
    assert "→ Менеджер (Ольга): Здравствуйте, Анна!" in out
    assert "=== Примечание в сделке #1234 · AI-помощник ===" in out
    # Ожидаемый черновик — из записанного ответа: его можно перезаписать другой моделью.
    recording = json.loads(
        (BASE_DIR / "examples" / "mock_llm" / "01-price-install.json").read_text(encoding="utf-8")
    )
    assert f"Черновик ответа клиенту:\n{recording['suggestion']['client_reply']}" in out
    assert "Допродажа (только для менеджера) · предложить сейчас" in out


def test_single_message_and_outgoing_only(service, capsys):
    assert run(service, "--lead", "1236", "--text", "Сколько длится монтаж?") == 0
    assert "стандартный монтаж занимает 3–4 часа" in capsys.readouterr().out.lower()
    assert run(service, "--lead", "1236", "--text", "Добрый день!", "--outgoing") == 0
    assert "подсказка не запускается" in capsys.readouterr().out


def test_bad_arguments(service, capsys):
    assert run(service, "--scenario", "nope") == 2
    assert "Есть: price-install" in capsys.readouterr().err
    assert run(service, "--lead", "424242", "--text", "?") == 2
    assert run(service, "--lead", "1234", "--text", "?", "--secret", "wrong") == 1


async def test_register_webhook(tmp_path):
    fake = FakeAmoApi.from_file(SEED, token="live-token")
    settings = make_settings(
        tmp_path,
        amocrm_mode="live",
        amocrm_subdomain="klimat-demo",
        amocrm_token="live-token",
        webhook_secret="s3",
    )
    destination = await register("https://abc.trycloudflare.com/", settings, fake.transport())
    assert destination == "https://abc.trycloudflare.com/webhooks/amocrm/s3"
    assert fake.webhooks == [
        {"destination": destination, "settings": ["add_message", "add_outgoing_message"]}
    ]

    with pytest.raises(ValueError, match="https"):
        await register("http://abc", settings, fake.transport())
    with pytest.raises(ValueError, match="AMOCRM_MODE=live"):
        await register("https://abc", make_settings(tmp_path), fake.transport())


# ---------- Регистрация: «Invalid URL» со свежим доменом ----------

HOOK_SECRET = "hook-secret-XYZ"
# Так amoCRM отвечает, когда не нашёл домен адреса в DNS (у только что созданного туннеля бывает).
INVALID_URL = {
    "validation-errors": [
        {
            "request_id": "0",
            "errors": [
                {
                    "code": "57c2f299-1154-4870-89bb-ef3b1f5ad229",
                    "path": "destination.destination",
                    "detail": "Invalid URL",
                }
            ],
        }
    ],
    "title": "Bad Request",
    "status": 400,
    "detail": "Request validation failed",
}


def live_settings(tmp_path, **overrides):
    values = {
        "amocrm_mode": "live",
        "amocrm_subdomain": "klimat-demo",
        "amocrm_token": "live-token",
        "webhook_secret": HOOK_SECRET,
    }
    values.update(overrides)
    return make_settings(tmp_path, **values)


def flaky_webhooks(fake: FakeAmoApi, failures: int) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    """Первые failures попыток регистрации вебхука получают «Invalid URL», остальное — поддельный amoCRM."""
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v4/webhooks":
            calls.append(request)
            if len(calls) <= failures:
                return httpx.Response(400, json=INVALID_URL)
        return fake.handle(request)

    return httpx.MockTransport(handle), calls


async def test_register_retries_invalid_url(tmp_path):
    fake = FakeAmoApi.from_file(SEED, token="live-token")
    transport, calls = flaky_webhooks(fake, failures=2)
    retries: list[tuple[int, float]] = []
    destination = await register(
        "https://abc.lhr.life",
        live_settings(tmp_path),
        transport,
        retry_delays=(0, 0.01, 0),
        on_retry=lambda attempt, delay: retries.append((attempt, delay)),
    )
    assert destination == f"https://abc.lhr.life/webhooks/amocrm/{HOOK_SECRET}"
    assert len(calls) == 3 and retries == [(2, 0), (3, 0.01)]  # номер следующей попытки и пауза перед ней
    assert [w["destination"] for w in fake.webhooks] == [destination]


async def test_register_gives_up_with_explanation(tmp_path):
    fake = FakeAmoApi.from_file(SEED, token="live-token")
    transport, calls = flaky_webhooks(fake, failures=99)
    with pytest.raises(AmoRequestError, match="ищет домен в DNS") as excinfo:
        await register("https://abc.lhr.life/", live_settings(tmp_path), transport, retry_delays=(0, 0))
    assert len(calls) == 3 and fake.webhooks == []
    assert "адрес https://abc.lhr.life («Invalid URL»)" in str(excinfo.value)
    assert HOOK_SECRET not in str(excinfo.value)


async def test_register_does_not_retry_other_errors(tmp_path):
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(400, json={"title": "Bad Request", "detail": "unknown event add_nothing"})

    with pytest.raises(AmoRequestError, match="unknown event"):
        await register(
            "https://abc.lhr.life", live_settings(tmp_path), httpx.MockTransport(handle), retry_delays=(0,)
        )
    assert len(calls) == 1


# ---------- Проверка адреса перед регистрацией ----------


@pytest.fixture
def live_service(tmp_path):
    fake = FakeAmoApi.from_file(SEED, token="live-token")
    settings = live_settings(tmp_path, worker_enabled=False)
    app = create_app(settings, llm=FakeLLM(make_suggestion()), amo_transport=fake.transport())
    with TestClient(app) as client:
        yield client, settings


def tunnel_to(client: TestClient) -> httpx.MockTransport:
    """Как туннель: запросы на любой публичный адрес уходят приложению."""

    def handle(request: httpx.Request) -> httpx.Response:
        content_type = (
            {"content-type": request.headers["content-type"]} if "content-type" in request.headers else {}
        )
        response = client.request(
            request.method, request.url.path, content=request.content, headers=content_type
        )
        return httpx.Response(
            response.status_code,
            content=response.content,
            headers={"content-type": response.headers.get("content-type", "")},
        )

    return httpx.MockTransport(handle)


async def test_check_destination_accepts_live_service_with_same_secret(live_service, tmp_path):
    client, settings = live_service
    assert await check_destination("https://abc.lhr.life/", settings, tunnel_to(client)) is None
    assert client.get("/health").json()["queue"] == {}  # пустая посылка ничего не ставит в очередь

    other = live_settings(tmp_path, webhook_secret="another-secret")
    problem = await check_destination("https://abc.lhr.life", other, tunnel_to(client))
    assert problem is not None and "WEBHOOK_SECRET" in problem and "another-secret" not in problem


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (httpx.Response(200, json={"status": "ok", "amocrm": "mock"}), "AMOCRM_MODE=mock"),
        (
            httpx.Response(
                200, json={"status": "degraded", "amocrm": "live", "amocrm_problem": "Token expired"}
            ),
            "Token expired",
        ),
        (httpx.Response(530, text="<html>Cloudflare Tunnel error</html>"), "не ответ сервиса"),
        (httpx.Response(404, text="no tunnel here"), "не ответ сервиса"),
    ],
)
async def test_check_destination_problems(tmp_path, response, expected):
    def handle(request: httpx.Request) -> httpx.Response:
        return response

    problem = await check_destination(
        "https://abc.lhr.life", live_settings(tmp_path), httpx.MockTransport(handle)
    )
    assert problem is not None and expected in problem


async def test_check_destination_tunnel_down(tmp_path):
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    problem = await check_destination(
        "https://abc.lhr.life", live_settings(tmp_path), httpx.MockTransport(handle)
    )
    assert problem is not None and "туннель не запущен" in problem


def test_setup_webhook_cli_validates_before_network(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(setup_webhook, "get_settings", lambda: make_settings(tmp_path))

    async def fail(*args, **kwargs):
        raise AssertionError("сеть не должна вызываться")

    monkeypatch.setattr(setup_webhook, "check_destination", fail)
    assert setup_webhook.main(["https://abc.lhr.life"]) == 2
    assert "AMOCRM_MODE=live" in capsys.readouterr().err


def test_setup_webhook_cli_stops_on_failed_check(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(setup_webhook, "get_settings", lambda: live_settings(tmp_path))

    async def problem(*args, **kwargs):
        return "туннель не запущен"

    async def fail(*args, **kwargs):
        raise AssertionError("регистрации не должно быть")

    monkeypatch.setattr(setup_webhook, "check_destination", problem)
    monkeypatch.setattr(setup_webhook, "register", fail)
    assert setup_webhook.main(["https://abc.lhr.life"]) == 1
    err = capsys.readouterr().err
    assert "туннель не запущен" in err and "--no-check" in err


def test_default_retry_window_is_about_a_minute():
    # Свежий адрес туннеля amoCRM на живом аккаунте находил в DNS только примерно через минуту.
    assert 5 <= len(setup_webhook.INVALID_URL_RETRY_DELAYS) + 1 <= 6
    assert 50 <= sum(setup_webhook.INVALID_URL_RETRY_DELAYS) <= 90


def test_setup_webhook_cli_reports_retries_and_asks_for_probe(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(setup_webhook, "get_settings", lambda: live_settings(tmp_path))

    async def ok(*args, **kwargs):
        return None

    async def register_after_retry(public_url, settings, *args, on_retry=None, **kwargs):
        on_retry(2, 5.0)
        return setup_webhook.webhook_destination(public_url, settings)

    monkeypatch.setattr(setup_webhook, "check_destination", ok)
    monkeypatch.setattr(setup_webhook, "register", register_after_retry)
    assert setup_webhook.main(["https://abc.lhr.life"]) == 0
    out, err = capsys.readouterr()
    assert "Вебхук зарегистрирован: https://abc.lhr.life/webhooks/amocrm/***" in out
    assert HOOK_SECRET not in out + err
    assert "повтор через 5 с (попытка 2 из 5)" in err
    assert "пробное сообщение" in out

    async def register_first_try(public_url, settings, *args, **kwargs):
        return setup_webhook.webhook_destination(public_url, settings)

    monkeypatch.setattr(setup_webhook, "register", register_first_try)
    assert setup_webhook.main(["https://abc.lhr.life"]) == 0
    out, err = capsys.readouterr()
    assert "повтор" not in err and "пробное сообщение" in out
