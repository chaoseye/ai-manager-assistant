"""Mock-LLM и демо-сценарии: записанные ответы должны проходить все проверки на текущей БЗ."""

import pytest

from app.config import BASE_DIR
from app.core.assistant import Assistant
from app.core.guards import apply_guards
from app.core.mock_llm import MockLLMClient, faq_fallback, load_recordings, normalize
from app.core.schemas import LeadContext, SuggestRequest
from app.scenarios import load_scenarios
from tests.conftest import make_settings

SCENARIOS = load_scenarios(BASE_DIR / "examples" / "scenarios")
RECORDINGS = load_recordings(BASE_DIR / "examples" / "mock_llm")
# Сценарий с намеренной ошибкой в цене: на нём проверка цен должна сработать.
GUARD_DEMO = "price-check"


def split_dialog(scenario):
    dialog = scenario.dialog
    start = len(dialog)
    while start > 0 and dialog[start - 1].role == "client":
        start -= 1
    return dialog[:start], "\n".join(m.text for m in dialog[start:])


def test_every_scenario_has_a_recording():
    assert len(SCENARIOS) == 8
    for scenario in SCENARIOS:
        _, message = split_dialog(scenario)
        assert normalize(message) in RECORDINGS, scenario.id


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.id)
def test_recordings_pass_guards(kb, scenario):
    history, message = split_dialog(scenario)
    request = SuggestRequest(message=message, history=history, lead=scenario.lead, channel=scenario.channel)
    recording = RECORDINGS[normalize(message)]
    result, warnings = apply_guards(recording.suggestion, kb, request, "full")
    if scenario.id == GUARD_DEMO:
        assert [w.code for w in warnings] == ["price_not_in_kb"]
        assert "52 900 ₽" in warnings[0].message  # ошибочная цена; итог 62 800 ₽ выводится из неё и монтажа
        assert result.needs_human is True
        return
    assert warnings == [], [w.message for w in warnings]
    assert result == recording.suggestion


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.id)
async def test_scenarios_end_to_end_in_mock_mode(kb_store, tmp_path, scenario):
    settings = make_settings(tmp_path)
    assistant = Assistant(kb_store, MockLLMClient(settings.mock_llm_dir), settings)
    history, message = split_dialog(scenario)
    result = await assistant.suggest(
        SuggestRequest(message=message, history=history, lead=scenario.lead, channel=scenario.channel)
    )
    assert result.meta.llm_mode == "mock"
    assert (result.meta.warnings != []) is (scenario.id == GUARD_DEMO)
    assert result.suggestion.client_reply


def test_expected_behaviour_of_key_scenarios():
    by_id = {r.id: r.suggestion for r in RECORDINGS.values()}
    assert by_id["out-of-kb"].needs_human and not by_id["out-of-kb"].answer_found_in_kb
    assert by_id["complaint"].upsell.timing == "not_now"
    assert "service-1y" not in by_id["declined-upsell"].upsell.product_ids
    assert "90%" not in by_id["prompt-injection"].client_reply
    assert by_id["after-sale"].upsell.product_ids == ["service-1y"]


def test_normalize_ignores_case_punctuation_and_yo():
    assert normalize("Ребёнок,  ВАЖНО!") == normalize("ребенок важно")


def test_faq_fallback_finds_similar_question(kb):
    suggestion = faq_fallback("Подскажите, сколько длится монтаж?", kb, "Анна")
    assert suggestion.kb_refs == ["faq-install-duration"]
    assert suggestion.client_reply.startswith("Анна, стандартный монтаж занимает")
    assert suggestion.needs_human is False


def test_faq_fallback_unknown_question(kb):
    suggestion = faq_fallback("Можно оплатить биткоинами?", kb, None)
    assert suggestion.needs_human is True
    assert suggestion.kb_refs == []
    assert suggestion.upsell.recommended is False


async def test_mock_client_uses_lead_name(kb_store, tmp_path):
    settings = make_settings(tmp_path)
    assistant = Assistant(kb_store, MockLLMClient(settings.mock_llm_dir), settings)
    result = await assistant.suggest(
        SuggestRequest(message="Будет ли пыль при монтаже?", lead=LeadContext(contact_name="Олег"))
    )
    assert result.suggestion.client_reply.startswith("Олег, мастера работают")


def test_faq_fallback_does_not_match_long_unrelated_message(kb):
    suggestion = faq_fallback("Кондиционер после установки гудит и вибрирует, спать невозможно.", kb, None)
    assert suggestion.kb_refs == []
    assert suggestion.needs_human is True


def test_faq_fallback_does_not_answer_price_with_duration(kb):
    # «сколько» и «монтаж» совпадают с «сколько длится монтаж», но вопрос о цене — лучше честное «уточню».
    suggestion = faq_fallback("Сколько стоит монтаж?", kb, None)
    assert suggestion.kb_refs == [] and suggestion.needs_human is True


def test_light_modules_do_not_import_model_sdk():
    import subprocess
    import sys

    code = "import sys, app.amocrm.simulate, app.cli; print('anthropic' in sys.modules)"
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "False"
