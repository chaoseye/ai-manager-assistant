"""evals.record_scenarios: запрос как на демо-странице, запись только ответов, прошедших проверки."""

import json
import shutil

import pytest

from app.config import BASE_DIR, get_settings
from app.core.assistant import Assistant
from app.core.llm_registry import LLMRegistry
from app.core.mock_llm import MockLLMClient
from app.core.schemas import SuggestRequest
from app.scenarios import load_scenarios
from evals import record_scenarios
from tests.conftest import FakeLLM, make_settings, make_suggestion

SCENARIOS = {s.id: s for s in load_scenarios(BASE_DIR / "examples" / "scenarios")}


def test_request_is_built_like_the_demo_page():
    request = record_scenarios.build_request(SCENARIOS["price-install"])
    assert request.message.startswith("20 метров")
    assert [m.role for m in request.history] == ["client", "manager"]
    assert request.lead.contact_name == "Анна" and request.channel == "telegram"


@pytest.fixture
def live_env(tmp_path, monkeypatch):
    """Копия записей во временном каталоге, LLM_MODE=live и поддельная модель вместо шлюза."""
    recordings = tmp_path / "mock_llm"
    shutil.copytree(BASE_DIR / "examples" / "mock_llm", recordings)
    for key, value in {"LLM_MODE": "live", "MOCK_LLM_DIR": str(recordings), "LLM_PROVIDER": "grok"}.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    yield recordings
    get_settings.cache_clear()


def use_llm(monkeypatch, llm):
    llm.model = "grok-4.7"
    monkeypatch.setattr(record_scenarios, "build_llms", lambda settings: LLMRegistry({"grok": llm}, "grok"))


def test_records_passing_answers_and_keeps_handcrafted(live_env, monkeypatch, capsys):
    reply = "Здравствуйте! Стандартный монтаж стоит 9 900 ₽. Когда вам удобно принять мастера?"
    use_llm(monkeypatch, FakeLLM(make_suggestion(client_reply=reply)))
    before = (live_env / "08-price-check.json").read_text(encoding="utf-8")

    assert record_scenarios.main(["--only", "discount,price-check"]) == 0
    saved = json.loads((live_env / "04-discount.json").read_text(encoding="utf-8"))
    assert saved["id"] == "discount" and saved["model"] == "grok-4.7" and saved["recorded_at"]
    assert saved["suggestion"]["client_reply"] == reply
    assert (live_env / "08-price-check.json").read_text(encoding="utf-8") == before  # ручной не трогаем
    assert "price-check: ответ составлен вручную" in capsys.readouterr().out


def test_answer_with_guard_warnings_is_not_saved(live_env, monkeypatch, capsys):
    use_llm(monkeypatch, FakeLLM(make_suggestion(client_reply="Монтаж стоит 7 777 ₽.")))
    before = (live_env / "04-discount.json").read_text(encoding="utf-8")
    assert record_scenarios.main(["--only", "discount"]) == 1
    assert (live_env / "04-discount.json").read_text(encoding="utf-8") == before
    assert "не прошёл проверки" in capsys.readouterr().out


def test_requires_live_mode(monkeypatch, capsys):
    monkeypatch.setenv("LLM_MODE", "mock")
    get_settings.cache_clear()
    try:
        assert record_scenarios.main([]) == 2
    finally:
        get_settings.cache_clear()
    assert "LLM_MODE=live" in capsys.readouterr().err


async def test_mock_shows_which_model_recorded_the_answer(kb_store, tmp_path):
    settings = make_settings(tmp_path)
    assistant = Assistant(kb_store, MockLLMClient(settings.mock_llm_dir), settings)
    request = record_scenarios.build_request(SCENARIOS["complaint"])
    result = await assistant.suggest(request)
    assert result.meta.model == "grok-4.7 (запись)" and result.meta.llm_mode == "mock"
    # Ответ по FAQ модель не давала — остаётся просто mock.
    faq = await assistant.suggest(SuggestRequest(message="Сколько длится монтаж?"))
    assert faq.meta.model == "mock"
