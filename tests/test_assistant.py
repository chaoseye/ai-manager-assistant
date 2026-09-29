import pytest

from app.core.assistant import Assistant
from app.core.llm import LLMBadOutputError, LLMRefusedError, LLMTruncatedError, LLMUnavailableError
from app.core.schemas import DialogMessage, SuggestRequest, Usage
from tests.conftest import FakeLLM, make_settings, make_suggestion

REQUEST = SuggestRequest(message="Сколько стоит монтаж?")


def make_assistant(kb_store, tmp_path, llm, **overrides):
    return Assistant(kb_store, llm, make_settings(tmp_path, **overrides))


async def test_happy_path_meta_and_prompts(kb_store, tmp_path):
    llm = FakeLLM(make_suggestion())
    result = await make_assistant(kb_store, tmp_path, llm).suggest(REQUEST)
    assert result.suggestion.client_reply.startswith("Стандартный монтаж")
    meta = result.meta
    assert meta.mode == "full"
    assert meta.llm_mode == "fake" and meta.model == "fake-model"
    assert meta.kb_version == kb_store.current.version
    assert meta.attempts == 1
    assert meta.usage == Usage(input_tokens=100, output_tokens=50)
    assert meta.warnings == []
    assert len(meta.suggestion_id) == 32

    call = llm.calls[0]
    assert call.max_tokens == 8000
    assert 'id="install-standard"' in call.system
    assert "Сколько стоит монтаж?" in call.user


async def test_system_prompt_is_reused_until_kb_changes(kb_store, tmp_path):
    llm = FakeLLM(make_suggestion())
    assistant = make_assistant(kb_store, tmp_path, llm)
    await assistant.suggest(REQUEST)
    await assistant.suggest(REQUEST)
    assert llm.calls[0].system is llm.calls[1].system


async def test_pii_is_masked_and_history_limited(kb_store, tmp_path):
    llm = FakeLLM(make_suggestion())
    request = SuggestRequest(
        message="Перезвоните на +7 916 123-45-67",
        history=[DialogMessage(role="client", text=f"сообщение {i}") for i in range(30)],
    )
    await make_assistant(kb_store, tmp_path, llm, history_limit=5).suggest(request)
    call = llm.calls[0]
    assert "[PHONE]" in call.user and "916" not in call.user
    assert len(call.request.history) == 5
    assert "сообщение 29" in call.user and "сообщение 24" not in call.user


async def test_pii_masking_can_be_disabled(kb_store, tmp_path):
    llm = FakeLLM(make_suggestion())
    await make_assistant(kb_store, tmp_path, llm, pii_masking=False).suggest(
        SuggestRequest(message="Мой телефон 89161234567")
    )
    assert "89161234567" in llm.calls[0].user


async def test_guards_are_applied(kb_store, tmp_path):
    llm = FakeLLM(make_suggestion(client_reply="Монтаж стоит 7 777 ₽."))
    result = await make_assistant(kb_store, tmp_path, llm).suggest(REQUEST)
    assert [w.code for w in result.meta.warnings] == ["price_not_in_kb"]
    assert result.suggestion.needs_human is True


async def test_truncated_answer_is_retried_with_doubled_limit(kb_store, tmp_path):
    llm = FakeLLM(LLMTruncatedError("обрезано", Usage(output_tokens=8000)), make_suggestion())
    result = await make_assistant(kb_store, tmp_path, llm).suggest(REQUEST)
    assert [c.max_tokens for c in llm.calls] == [8000, 16000]
    assert result.meta.attempts == 2
    assert result.meta.usage.output_tokens == 8050  # токены неудачной попытки тоже учтены


async def test_truncated_twice_raises(kb_store, tmp_path):
    llm = FakeLLM(LLMTruncatedError("обрезано"), LLMTruncatedError("обрезано"))
    with pytest.raises(LLMTruncatedError):
        await make_assistant(kb_store, tmp_path, llm).suggest(REQUEST)


async def test_empty_reply_is_retried_once(kb_store, tmp_path):
    llm = FakeLLM(make_suggestion(client_reply="  "), make_suggestion())
    result = await make_assistant(kb_store, tmp_path, llm).suggest(REQUEST)
    assert result.meta.attempts == 2
    assert result.suggestion.client_reply

    llm = FakeLLM(make_suggestion(client_reply=""), make_suggestion(client_reply=""))
    with pytest.raises(LLMBadOutputError, match="пустой"):
        await make_assistant(kb_store, tmp_path, llm).suggest(REQUEST)


async def test_bad_output_retried_once(kb_store, tmp_path):
    llm = FakeLLM(LLMBadOutputError("не JSON"), make_suggestion())
    result = await make_assistant(kb_store, tmp_path, llm).suggest(REQUEST)
    assert result.meta.attempts == 2


@pytest.mark.parametrize("error", [LLMRefusedError("отказ"), LLMUnavailableError("нет сети")])
async def test_refusal_and_unavailable_are_not_retried_here(kb_store, tmp_path, error):
    llm = FakeLLM(error)
    with pytest.raises(type(error)):
        await make_assistant(kb_store, tmp_path, llm).suggest(REQUEST)
    assert len(llm.calls) == 1


async def test_upsell_only_mode_accepts_empty_reply(kb_store, tmp_path):
    llm = FakeLLM(make_suggestion(client_reply="", kb_refs=[]))
    result = await make_assistant(kb_store, tmp_path, llm).suggest(REQUEST, mode="upsell_only")
    assert result.meta.mode == "upsell_only"
    assert result.meta.attempts == 1
    assert "черновик ответа не нужен" in llm.calls[0].user


async def test_replies_are_masked(kb_store, tmp_path):
    llm = FakeLLM(make_suggestion(client_reply="", kb_refs=[]))
    request = SuggestRequest(
        message="Сколько стоит?", replies=[DialogMessage(role="manager", text="Звоните +7 916 123-45-67")]
    )
    await make_assistant(kb_store, tmp_path, llm).suggest(request, mode="upsell_only")
    assert "[PHONE]" in llm.calls[0].user and "916" not in llm.calls[0].user
