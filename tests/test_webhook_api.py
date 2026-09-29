"""Вебхук и amoCRM на уровне HTTP: секрет, аккаунт, формат, весь путь до примечания, health, настройки."""

from urllib.parse import urlencode

import httpx
import pytest
from fastapi.testclient import TestClient

from app.amocrm.webhooks import encode_nested_form
from app.main import ConfigError, create_app
from tests.conftest import FakeLLM, make_settings, make_suggestion

SECRET = "hook-secret"


def mock_settings(tmp_path, **overrides):
    values = {
        "amocrm_mode": "mock",
        "webhook_secret": SECRET,
        "debounce_seconds": 0,
        "worker_enabled": False,  # обработку запускаем вручную через worker.tick()
    }
    values.update(overrides)
    return make_settings(tmp_path, **values)


def webhook_body(text="Сколько стоит монтаж?", msg_id="m1", lead="1234", account_id="29000001") -> bytes:
    data = {
        "message": {
            "add": [
                {
                    "id": msg_id,
                    "chat_id": "chat-42",
                    "talk_id": "7",
                    "contact_id": "3001234",
                    "author": {"id": "x", "type": "external", "name": "Анна"},
                    "text": text,
                    "created_at": "1790600000",
                    "origin": "telegram",
                    "element_id": lead,
                    "element_type": "2",
                }
            ]
        },
        "account": {"id": account_id, "subdomain": "klimat-demo"},
    }
    return urlencode(encode_nested_form(data)).encode()


def post_hook(client: TestClient, body: bytes, secret: str = SECRET) -> httpx.Response:
    return client.post(
        f"/webhooks/amocrm/{secret}",
        content=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )


@pytest.fixture
def mock_client(tmp_path):
    app = create_app(mock_settings(tmp_path), llm=FakeLLM(make_suggestion()))
    with TestClient(app) as client:
        yield client


def test_webhook_disabled_when_amocrm_off(client):
    response = post_hook(client, webhook_body())
    assert response.status_code == 404
    assert "AMOCRM_MODE=off" in response.json()["error"]["message"]


def test_wrong_secret(mock_client):
    assert post_hook(mock_client, webhook_body(), secret="nope").status_code == 404


def test_full_path_to_note(mock_client):
    response = post_hook(mock_client, webhook_body())
    assert response.status_code == 200
    assert response.json() == {"ok": True, "accepted": 1, "duplicates": 0, "scheduled": 1, "skipped": 0}
    assert post_hook(mock_client, webhook_body()).json()["duplicates"] == 1

    app = mock_client.app
    assert mock_client.portal.call(app.state.worker.tick) == 1
    notes = mock_client.get(
        "/api/v1/amocrm-mock/notes", params={"entity_type": "leads", "entity_id": 1234}
    ).json()
    assert len(notes) == 1
    assert notes[0]["params"]["text"].startswith("Черновик ответа клиенту:")
    assert mock_client.get("/api/v1/amocrm-mock/notes", params={"after_id": notes[0]["id"]}).json() == []

    health = mock_client.get("/health").json()
    assert health["status"] == "ok"
    assert health["amocrm"] == "mock"
    assert health["queue"] == {"done": 1}
    assert health["worker"] == "stopped"


def test_bad_body_and_foreign_account(tmp_path):
    settings = mock_settings(tmp_path, amocrm_account_id=29000001)
    with TestClient(create_app(settings, llm=FakeLLM(make_suggestion()))) as client:
        bad = client.post(
            f"/webhooks/amocrm/{SECRET}", content=b"{oops", headers={"Content-Type": "application/json"}
        )
        assert bad.status_code == 200 and bad.json()["accepted"] == 0
        foreign = post_hook(client, webhook_body(account_id="1"))
        assert foreign.status_code == 200 and foreign.json()["accepted"] == 0
        assert post_hook(client, webhook_body()).json()["accepted"] == 1


def test_mock_leads_endpoint(mock_client):
    leads = mock_client.get("/api/v1/amocrm-mock/leads").json()
    anna = next(lead for lead in leads if lead["id"] == 1234)
    assert anna == {
        "id": 1234,
        "name": "Кондиционер в спальню",
        "stage": "Первичный контакт",
        "contact_id": 3001234,
        "contact_name": "Анна",
    }


def test_mock_endpoints_hidden_outside_mock_mode(client):
    assert client.get("/api/v1/amocrm-mock/notes").status_code == 404


def test_background_worker_is_started(tmp_path):
    settings = mock_settings(tmp_path, worker_enabled=True, worker_poll_seconds=0.05)
    with TestClient(create_app(settings, llm=FakeLLM(make_suggestion()))) as client:
        assert client.get("/health").json()["worker"] == "running"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"amocrm_mode": "mock", "webhook_secret": None}, "WEBHOOK_SECRET"),
        ({"amocrm_mode": "live", "webhook_secret": "s"}, "AMOCRM_SUBDOMAIN"),
        ({"amocrm_mode": "live", "webhook_secret": "s", "amocrm_subdomain": "x"}, "AMOCRM_TOKEN"),
    ],
)
def test_incomplete_config_prevents_startup(tmp_path, overrides, message):
    with (
        pytest.raises(ConfigError, match=message),
        TestClient(create_app(make_settings(tmp_path, **overrides))),
    ):
        pass


def test_live_mode_with_rejected_token_is_degraded(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"title": "Unauthorized", "detail": "Token expired"})

    settings = make_settings(
        tmp_path,
        amocrm_mode="live",
        amocrm_subdomain="klimat-demo",
        amocrm_token="expired",
        webhook_secret=SECRET,
        worker_enabled=False,
    )
    app = create_app(settings, llm=FakeLLM(make_suggestion()), amo_transport=httpx.MockTransport(handler))
    with TestClient(app) as client:
        health = client.get("/health").json()
    assert health["status"] == "degraded"
    assert "Token expired" in health["amocrm_problem"]
