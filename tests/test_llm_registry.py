"""Реестр моделей: маршруты, запасные модели, выбор модели в ядре, API и на демо-странице."""

import pytest
from fastapi.testclient import TestClient

from app.config import PROVIDER_IDS, Settings
from app.core.assistant import Assistant
from app.core.gateway_llm import NO_GATEWAY
from app.core.llm import LLMBadOutputError, LLMUnavailableError
from app.core.llm_registry import LLMRegistry, build_llms
from app.core.providers import PROVIDERS
from app.core.schemas import SuggestRequest
from app.main import create_app
from tests.conftest import FakeLLM, make_settings, make_suggestion

REQUEST = SuggestRequest(message="Сколько стоит монтаж?")


def fake(model: str, *outcomes, problem: str | None = None) -> FakeLLM:
    llm = FakeLLM(*(outcomes or (make_suggestion(),)))
    llm.model = model
    llm.problem = problem
    return llm


def registry(**overrides) -> tuple[LLMRegistry, dict[str, FakeLLM]]:
    clients = {"claude": fake("claude-opus-5"), "kimi": fake("kimi-k3"), "glm": fake("glm-5.3")}
    clients.update(overrides)
    return LLMRegistry(clients, "claude", ["glm", "kimi", "claude", "glm"]), clients


# ---------- Настройки и таблица ----------


def test_provider_table_matches_settings():
    assert tuple(PROVIDERS) == PROVIDER_IDS


def test_fallback_list_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_FALLBACK_PROVIDERS", "glm, qwen")
    assert Settings(_env_file=None).llm_fallback_providers == ["glm", "qwen"]
    monkeypatch.setenv("LLM_FALLBACK_PROVIDERS", "")
    assert Settings(_env_file=None).llm_fallback_providers == []
    monkeypatch.setenv("LLM_FALLBACK_PROVIDERS", "gpt")
    with pytest.raises(ValueError, match="llm_fallback_providers"):
        Settings(_env_file=None)


def test_gateway_url_is_normalized(tmp_path):
    settings = make_settings(tmp_path, llm_gateway_url=" https://gw.test/v1/ ", llm_gateway_key="k")
    assert settings.llm_gateway_url == "https://gw.test/v1" and settings.gateway_configured
    assert not make_settings(
        tmp_path, llm_gateway_url="https://gw.test/v1", llm_gateway_key=" "
    ).gateway_configured


# ---------- Сборка реестра ----------


def test_mock_mode_answers_for_every_model(tmp_path):
    llms = build_llms(make_settings(tmp_path, llm_provider="kimi"))
    assert llms.ids() == list(PROVIDER_IDS)
    assert llms.default == "kimi"
    assert {llms.get(p).mode for p in PROVIDER_IDS} == {"mock"}


def test_live_through_gateway_only(tmp_path):
    settings = make_settings(
        tmp_path,
        llm_mode="live",
        llm_gateway_url="https://gw.test/v1",
        llm_gateway_key="k",
        anthropic_api_key=None,
    )
    llms = build_llms(settings)
    described = {item["id"]: item for item in llms.describe()}
    assert {item["route"] for item in described.values()} == {"gateway"}
    assert all(item["available"] for item in described.values())
    assert described["claude"]["model"] == "claude-opus-5"
    assert described["qwen"]["model"] == "qwen3.8-max"
    assert described["claude"]["default"] is True


def test_live_claude_through_anthropic_when_key_is_set(tmp_path):
    settings = make_settings(
        tmp_path,
        llm_mode="live",
        llm_gateway_url="https://gw.test/v1",
        llm_gateway_key="k",
        anthropic_api_key="sk-a",
    )
    llms = build_llms(settings)
    assert llms.get("claude").route == "anthropic" and llms.get("claude").model == "claude-opus-5-5"
    assert llms.get("grok").route == "gateway"


def test_live_without_gateway(tmp_path):
    llms = build_llms(make_settings(tmp_path, llm_mode="live", anthropic_api_key="sk-a"))
    assert llms.get("claude").route == "anthropic" and llms.get("claude").problem is None
    assert llms.get("deepseek").problem == NO_GATEWAY


def test_chain_and_fallbacks():
    llms, clients = registry()
    assert llms.fallbacks == ["glm", "kimi"]  # без повторов и без модели по умолчанию
    assert [p for p, _ in llms.chain(None)] == ["claude", "glm", "kimi"]
    assert llms.chain("kimi") == [("kimi", clients["kimi"])]
    assert llms.label("kimi") == "Kimi (kimi-k3)"
    with pytest.raises(ValueError):
        LLMRegistry({"glm": clients["glm"]}, "claude")


# ---------- Ядро ----------


async def test_selected_provider_answers(kb_store, settings):
    llms, clients = registry()
    result = await Assistant(kb_store, llms, settings).suggest(REQUEST, provider="kimi")
    assert result.meta.provider == "kimi" and result.meta.model == "kimi-k3"
    assert len(clients["kimi"].calls) == 1 and clients["claude"].calls == []


async def test_default_provider_and_single_client(kb_store, settings):
    result = await Assistant(kb_store, FakeLLM(make_suggestion()), settings).suggest(REQUEST)
    assert result.meta.provider == "claude"  # один клиент отвечает за LLM_PROVIDER


async def test_fallback_when_default_fails(kb_store, settings):
    llms, _ = registry(claude=fake("claude-opus-5", LLMUnavailableError("шлюз лежит")))
    result = await Assistant(kb_store, llms, settings).suggest(REQUEST)
    assert result.meta.provider == "glm"
    assert result.meta.attempts == 2
    notes = [w for w in result.meta.warnings if w.code == "llm_fallback"]
    assert len(notes) == 1
    assert "Claude (claude-opus-5)" in notes[0].message and "GLM (glm-5.3)" in notes[0].message


async def test_fallback_skips_unconfigured_and_reports_all_failures(kb_store, settings):
    llms, clients = registry(
        claude=fake("claude-opus-5", LLMBadOutputError("не JSON")),
        glm=fake("glm-5.3", problem=NO_GATEWAY),
    )
    result = await Assistant(kb_store, llms, settings).suggest(REQUEST)
    assert result.meta.provider == "kimi"
    assert clients["glm"].calls == []  # заведомо не ответит — не спрашиваем


async def test_no_fallback_for_explicit_choice(kb_store, settings):
    llms, clients = registry(kimi=fake("kimi-k3", LLMUnavailableError("нет канала")))
    with pytest.raises(LLMUnavailableError, match="нет канала"):
        await Assistant(kb_store, llms, settings).suggest(REQUEST, provider="kimi")
    assert clients["glm"].calls == []


async def test_all_failed_raises_last_error(kb_store, settings):
    llms, _ = registry(
        claude=fake("claude-opus-5", LLMUnavailableError("первая")),
        glm=fake("glm-5.3", LLMUnavailableError("вторая")),
        kimi=fake("kimi-k3", LLMUnavailableError("третья")),
    )
    with pytest.raises(LLMUnavailableError, match="третья"):
        await Assistant(kb_store, llms, settings).suggest(REQUEST)


# ---------- API и страница ----------


def test_suggest_with_provider_param(tmp_path):
    llms, _ = registry()
    with TestClient(create_app(make_settings(tmp_path), llm=llms)) as client:
        response = client.post("/api/v1/suggest?provider=kimi", json={"message": "Сколько стоит монтаж?"})
        assert response.status_code == 200
        assert response.json()["meta"]["provider"] == "kimi"
        saved = client.get(f"/api/v1/suggestions/{response.json()['meta']['suggestion_id']}").json()
        assert saved["meta"]["provider"] == "kimi"

        unknown = client.post("/api/v1/suggest?provider=gpt", json={"message": "?"})
        assert unknown.status_code == 422
        assert "gpt" in unknown.json()["error"]["message"]


def test_providers_endpoint_and_health(tmp_path):
    llms, _ = registry(kimi=fake("kimi-k3", problem=NO_GATEWAY))
    with TestClient(create_app(make_settings(tmp_path), llm=llms)) as client:
        body = client.get("/api/v1/llm/providers").json()
        health = client.get("/health").json()
    assert body["default"] == "claude" and body["fallbacks"] == ["glm", "kimi"]
    kimi = next(item for item in body["providers"] if item["id"] == "kimi")
    assert kimi["available"] is False and "LLM_GATEWAY_URL" in kimi["problem"]
    # Недоступная запасная модель не портит статус: модель по умолчанию работает.
    assert health["status"] == "ok" and health["llm_provider"] == "claude"
    assert health["llm_providers"]["kimi"] == {"model": "kimi-k3", "route": "fake", "available": False}


def test_mock_mode_has_no_model_picker(client):
    page = client.get("/").text
    assert 'id="llm-provider"' not in page and "LLM: mock" in page
    assert "llm_providers" not in client.get("/health").json()
    # Выбор модели в mock-режиме принимается: отвечает mock, а в meta видно, какую модель просили.
    meta = client.post("/api/v1/suggest?provider=grok", json={"message": "Сколько длится монтаж?"}).json()[
        "meta"
    ]
    assert meta["provider"] == "grok" and meta["llm_mode"] == "mock"


def test_live_page_shows_model_picker(tmp_path):
    llms, _ = registry(kimi=fake("kimi-k3", problem=NO_GATEWAY))
    with TestClient(create_app(make_settings(tmp_path), llm=llms)) as client:
        page = client.get("/").text
        amocrm_page = client.get("/amocrm").text
    assert 'id="llm-provider"' in page
    assert '<option value="claude" selected data-default="1">Claude · claude-opus-5</option>' in page
    assert '<option value="kimi" disabled>Kimi · kimi-k3 — недоступна</option>' in page
    # В сделках отвечает модель по умолчанию — там её просто показываем.
    assert 'id="llm-provider"' not in amocrm_page and "LLM: Claude (claude-opus-5)" in amocrm_page


# ---------- Проверка моделей шлюза ----------


def _gateway_settings(tmp_path, **overrides):
    values = {"llm_mode": "live", "llm_gateway_url": "https://gw.test/v1", "llm_gateway_key": "k"}
    values.update(overrides)
    return make_settings(tmp_path, **values)


async def test_check_gateway_marks_missing_models(tmp_path):
    import httpx

    ids = ["glm-5.3", "deepseek-v4-pro", "kimi-k3", "qwen3.8-max", "grok-4.6", "grok-4.7-fast"]
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"data": [{"id": i} for i in ids]})

    settings = _gateway_settings(tmp_path, llm_gateway_model_grok="grok-4.7")
    llms = build_llms(settings)
    await llms.check_gateway(settings, transport=httpx.MockTransport(handler))
    assert len(seen) == 1 and seen[0].url == "https://gw.test/v1/models"
    assert llms.gateway_check == "ok"
    assert (
        "нет в шлюзе" in llms.get("claude").problem
        and "LLM_GATEWAY_MODEL_CLAUDE" in llms.get("claude").problem
    )
    assert "grok-4.6, grok-4.7-fast" in llms.get("grok").problem  # подсказка по семейству
    assert llms.get("kimi").problem is None

    # Модель по умолчанию недоступна: запрос сразу получает понятную ошибку, шлюз не трогаем.
    with pytest.raises(LLMUnavailableError, match="нет в шлюзе"):
        await llms.get("claude").generate(None)


async def test_check_gateway_failure_changes_nothing(tmp_path):
    import httpx

    settings = _gateway_settings(tmp_path)
    llms = build_llms(settings)
    await llms.check_gateway(settings, transport=httpx.MockTransport(lambda r: httpx.Response(401, json={})))
    assert llms.gateway_check.startswith("не выполнена") and "ключ" in llms.gateway_check
    assert all(item["available"] for item in llms.describe())


async def test_check_gateway_skipped_without_gateway(tmp_path):
    llms = build_llms(make_settings(tmp_path))  # mock-режим
    await llms.check_gateway(make_settings(tmp_path))
    assert llms.gateway_check is None
