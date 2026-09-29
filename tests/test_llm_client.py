"""AnthropicLLMClient с подменённым SDK: параметры запроса и разбор ответа, без сети."""

from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from app.core.llm import (
    FALLBACK_BETA,
    SUGGESTION_SCHEMA,
    AnthropicLLMClient,
    LLMBadOutputError,
    LLMCall,
    LLMRefusedError,
    LLMTruncatedError,
    LLMUnavailableError,
)
from app.core.schemas import SuggestRequest
from tests.conftest import make_settings, make_suggestion


class FakeMessages:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.kwargs = None

    async def create(self, **kwargs):
        self.kwargs = kwargs
        if self.error:
            raise self.error
        return self.response


def fake_sdk(response=None, error=None):
    messages = FakeMessages(response, error)
    sdk = SimpleNamespace(
        beta=SimpleNamespace(messages=messages), api_key="sk-test", auth_token=None, credentials=None
    )
    return sdk, messages


def fake_response(text, stop_reason="end_turn", model="claude-opus-5-5", stop_details=None):
    return SimpleNamespace(
        content=[
            SimpleNamespace(type="thinking", thinking=""),
            SimpleNamespace(type="text", text=text),
        ],
        stop_reason=stop_reason,
        stop_details=stop_details,
        model=model,
        usage=SimpleNamespace(
            input_tokens=1200,
            output_tokens=900,
            cache_read_input_tokens=4100,
            cache_creation_input_tokens=None,
        ),
    )


def make_call(kb):
    return LLMCall(
        system="SYSTEM",
        user="USER",
        request=SuggestRequest(message="?"),
        kb=kb,
        mode="full",
        max_tokens=8000,
    )


async def test_request_params(kb, tmp_path):
    sdk, messages = fake_sdk(fake_response(make_suggestion().model_dump_json()))
    client = AnthropicLLMClient(make_settings(tmp_path, llm_mode="live", llm_effort="low"), client=sdk)
    await client.generate(make_call(kb))

    params = messages.kwargs
    assert params["model"] == "claude-opus-5-5"
    assert params["max_tokens"] == 8000
    assert params["system"] == [{"type": "text", "text": "SYSTEM", "cache_control": {"type": "ephemeral"}}]
    assert params["messages"] == [{"role": "user", "content": "USER"}]
    assert params["output_config"]["effort"] == "low"
    assert params["output_config"]["format"] == {"type": "json_schema", "schema": SUGGESTION_SCHEMA}
    assert params["betas"] == [FALLBACK_BETA]
    assert params["fallbacks"] == "default"
    assert "thinking" not in params  # у Opus 5.5 рассуждения адаптивные по умолчанию
    assert "temperature" not in params


async def test_fallbacks_can_be_disabled(kb, tmp_path):
    sdk, messages = fake_sdk(fake_response(make_suggestion().model_dump_json()))
    client = AnthropicLLMClient(make_settings(tmp_path, llm_fallbacks=False), client=sdk)
    await client.generate(make_call(kb))
    assert "fallbacks" not in messages.kwargs and "betas" not in messages.kwargs


def test_schema_is_strict():
    assert SUGGESTION_SCHEMA["additionalProperties"] is False
    assert set(SUGGESTION_SCHEMA["required"]) >= {"client_reply", "upsell", "kb_refs", "needs_human"}


async def test_parses_response_and_usage(kb, tmp_path):
    suggestion = make_suggestion()
    sdk, _ = fake_sdk(fake_response(suggestion.model_dump_json(), model="claude-opus-4-8"))
    response = await AnthropicLLMClient(make_settings(tmp_path), client=sdk).generate(make_call(kb))
    assert response.suggestion == suggestion
    assert response.model == "claude-opus-4-8"  # ответил fallback — фиксируем фактическую модель
    assert response.usage.cache_read_input_tokens == 4100
    assert response.usage.cache_creation_input_tokens == 0


async def test_refusal(kb, tmp_path):
    sdk, _ = fake_sdk(
        fake_response("", stop_reason="refusal", stop_details=SimpleNamespace(category="cyber"))
    )
    with pytest.raises(LLMRefusedError, match="cyber") as exc_info:
        await AnthropicLLMClient(make_settings(tmp_path), client=sdk).generate(make_call(kb))
    assert exc_info.value.usage.input_tokens == 1200


async def test_truncated(kb, tmp_path):
    sdk, _ = fake_sdk(fake_response('{"intent": "pri', stop_reason="max_tokens"))
    with pytest.raises(LLMTruncatedError):
        await AnthropicLLMClient(make_settings(tmp_path), client=sdk).generate(make_call(kb))


async def test_bad_json(kb, tmp_path):
    sdk, _ = fake_sdk(fake_response('{"intent": "price"}'))
    with pytest.raises(LLMBadOutputError):
        await AnthropicLLMClient(make_settings(tmp_path), client=sdk).generate(make_call(kb))


def _status_error(cls, status):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls("ошибка", response=httpx2.Response(status, request=request), body=None)


@pytest.mark.parametrize(
    "error",
    [
        _status_error(anthropic.RateLimitError, 429),
        _status_error(anthropic.InternalServerError, 500),
        _status_error(anthropic.AuthenticationError, 401),
        _status_error(anthropic.BadRequestError, 400),
        anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com")),
    ],
)
async def test_api_errors_become_unavailable(kb, tmp_path, error):
    sdk, _ = fake_sdk(error=error)
    with pytest.raises(LLMUnavailableError):
        await AnthropicLLMClient(make_settings(tmp_path), client=sdk).generate(make_call(kb))


async def test_missing_credentials_is_reported_before_request(kb, tmp_path):
    sdk, messages = fake_sdk(fake_response(make_suggestion().model_dump_json()))
    sdk.api_key = sdk.auth_token = sdk.credentials = None
    client = AnthropicLLMClient(make_settings(tmp_path), client=sdk)
    assert client.problem and "ANTHROPIC_API_KEY" in client.problem
    with pytest.raises(LLMUnavailableError, match="ANTHROPIC_API_KEY"):
        await client.generate(make_call(kb))
    assert messages.kwargs is None  # запрос не отправлялся


def test_real_sdk_client_without_key_has_problem(tmp_path, monkeypatch):
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    client = AnthropicLLMClient(make_settings(tmp_path, llm_mode="live", anthropic_api_key=None))
    if client._client.credentials is None:  # на машине может быть профиль `ant auth login`
        assert client.problem is not None
    keyed = AnthropicLLMClient(make_settings(tmp_path, llm_mode="live", anthropic_api_key="sk-test"))
    assert keyed.problem is None
