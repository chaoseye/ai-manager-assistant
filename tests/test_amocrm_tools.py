"""Имитатор amoCRM и регистрация вебхука.

Имитатор гоняется через TestClient: настоящие HTTP-запросы к приложению в mock-режиме, фоновый обработчик,
поддельный amoCRM и mock-LLM с записанными ответами — полный путь без сети и ключей.
"""

import pytest
from fastapi.testclient import TestClient

from app.amocrm import simulate
from app.amocrm.fake import FakeAmoApi
from app.amocrm.setup_webhook import register
from app.config import BASE_DIR
from app.main import create_app
from tests.conftest import make_settings

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
    assert (
        "Черновик ответа клиенту:\nАнна, для комнаты 20 м² подойдёт сплит-система Basic 09 — 32 900 ₽" in out
    )
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
