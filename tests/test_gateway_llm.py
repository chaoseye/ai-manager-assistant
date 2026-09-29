"""GatewayLLMClient на поддельном HTTP-транспорте: запрос, разбор ответа, упрощение запроса, ошибки."""

import json

import httpx
import pytest

from app.core.gateway_llm import (
    NO_GATEWAY,
    GatewayLLMClient,
    extract_json,
    format_instruction,
    parse_usage,
    request_levels,
    response_schema,
)
from app.core.llm import (
    LLMBadOutputError,
    LLMCall,
    LLMRefusedError,
    LLMTruncatedError,
    LLMUnavailableError,
)
from app.core.providers import PROVIDERS, map_effort
from app.core.schemas import SuggestRequest, Usage
from tests.conftest import make_settings, make_suggestion

GATEWAY = "https://gateway.test/v1"


class FakeGateway:
    """Отвечает по очереди заданными ответами и запоминает запросы."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        outcome = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    @property
    def bodies(self) -> list[dict]:
        return [json.loads(r.content) for r in self.requests]


def completion(content="", *, finish="stop", model="kimi-k3", usage=None, **message):
    body = {
        "id": "chatcmpl-1",
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content, **message},
                "finish_reason": finish,
            }
        ],
        "usage": usage
        or {
            "prompt_tokens": 5000,
            "completion_tokens": 700,
            "prompt_tokens_details": {"cached_tokens": 4000},
        },
    }
    return httpx.Response(200, json=body)


def error(status, message, code=""):
    return httpx.Response(status, json={"error": {"code": code, "message": message, "type": "new_api_error"}})


def make_client(tmp_path, provider="kimi", gateway=None, **overrides):
    values = {"llm_mode": "live", "llm_gateway_url": GATEWAY, "llm_gateway_key": "sk-test"}
    values.update(overrides)
    settings = make_settings(tmp_path, **values)
    transport = httpx.MockTransport(gateway) if gateway else None
    return GatewayLLMClient(settings, PROVIDERS[provider], transport=transport, backoff_seconds=0)


def make_call(kb, max_tokens=8000):
    return LLMCall(
        system="SYSTEM",
        user="USER",
        request=SuggestRequest(message="?"),
        kb=kb,
        mode="full",
        max_tokens=max_tokens,
    )


GOOD = make_suggestion().model_dump_json()


# ---------- Особенности моделей ----------


def test_effort_mapping():
    assert map_effort("medium", ("low", "high", "max")) == "low"
    assert map_effort("high", ("low", "high", "max")) == "high"
    assert map_effort("max", ("low", "medium", "high", "xhigh")) == "xhigh"
    assert map_effort("medium", ("low", "medium", "high", "xhigh")) == "medium"
    assert map_effort("low", ("high", "max")) == "high"  # ниже нет — берём самый низкий из доступных
    assert map_effort("high", ()) is None


def test_request_levels_go_from_full_to_simple():
    assert request_levels(PROVIDERS["kimi"]) == [
        ("json_schema", True),
        ("json_schema", False),
        ("json_object", False),
        (None, False),
    ]
    assert request_levels(PROVIDERS["glm"]) == [("json_object", True), ("json_object", False), (None, False)]
    # Claude через шлюз — без параметров рассуждений, так что уровня «с параметрами» нет.
    assert request_levels(PROVIDERS["claude"]) == [
        ("json_schema", False),
        ("json_object", False),
        (None, False),
    ]


def test_schema_is_strict_and_self_contained():
    schema = response_schema()
    text = json.dumps(schema)
    assert "$ref" not in text and "$defs" not in text and '"title"' not in text
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])
    upsell = schema["properties"]["upsell"]
    assert upsell["type"] == "object" and upsell["additionalProperties"] is False
    assert set(upsell["required"]) == set(upsell["properties"])
    assert schema["properties"]["upsell"]["properties"]["timing"]["enum"] == [
        "now",
        "after_resolution",
        "not_now",
    ]
    # Схема и пример — в системном промпте: DeepSeek в режиме json_object требует слово json и пример.
    instruction = format_instruction()
    assert "json" in instruction and '"client_reply"' in instruction


def test_body_per_provider(kb, tmp_path):
    kimi = make_client(tmp_path, "kimi").build_body(make_call(kb))
    assert kimi["model"] == "kimi-k3" and kimi["max_tokens"] == 8000
    assert kimi["response_format"]["type"] == "json_schema"
    assert kimi["response_format"]["json_schema"]["strict"] is True
    assert kimi["reasoning_effort"] == "low"  # LLM_EFFORT=medium → ближайший уровень Kimi
    assert "temperature" not in kimi  # у Kimi K3 температура зафиксирована
    system, user = kimi["messages"]
    assert (
        system["role"] == "system"
        and system["content"].startswith("SYSTEM")
        and "<response_format>" in system["content"]
    )
    assert user == {"role": "user", "content": "USER"}

    qwen = make_client(tmp_path, "qwen").build_body(make_call(kb))
    assert qwen["enable_thinking"] is False and "reasoning_effort" not in qwen
    assert qwen["response_format"]["type"] == "json_schema"

    glm = make_client(tmp_path, "glm").build_body(make_call(kb))
    assert glm["response_format"] == {"type": "json_object"} and glm["reasoning_effort"] == "low"

    grok = make_client(tmp_path, "grok", llm_effort="max").build_body(make_call(kb))
    assert grok["reasoning_effort"] == "xhigh"

    claude = make_client(tmp_path, "claude").build_body(make_call(kb))
    assert claude["model"] == "claude-opus-5" and "reasoning_effort" not in claude

    bare = make_client(tmp_path, "kimi").build_body(make_call(kb), level=3)
    assert "response_format" not in bare and "reasoning_effort" not in bare


def test_gateway_model_ids_are_configurable(kb, tmp_path):
    client = make_client(tmp_path, "deepseek", llm_gateway_model_deepseek="deepseek-v4-pro-0813")
    assert client.model == "deepseek-v4-pro-0813"
    assert client.build_body(make_call(kb))["model"] == "deepseek-v4-pro-0813"


# ---------- Успешный ответ ----------


async def test_generate_parses_response(kb, tmp_path):
    gateway = FakeGateway(completion(GOOD, reasoning_content="думаю…"))
    response = await make_client(tmp_path, gateway=gateway).generate(make_call(kb))

    assert response.suggestion == make_suggestion()
    assert response.model == "kimi-k3"
    assert response.usage == Usage(input_tokens=1000, output_tokens=700, cache_read_input_tokens=4000)
    request = gateway.requests[0]
    assert str(request.url) == f"{GATEWAY}/chat/completions"
    assert request.headers["authorization"] == "Bearer sk-test"


@pytest.mark.parametrize(
    "content",
    [
        f"```json\n{GOOD}\n```",
        f"<think>сначала подумаю</think>\n{GOOD}",
        f"Вот ответ:\n{GOOD}\nГотово.",
        [{"type": "text", "text": GOOD}],
    ],
)
async def test_content_variants(kb, tmp_path, content):
    response = await make_client(tmp_path, gateway=FakeGateway(completion(content))).generate(make_call(kb))
    assert response.suggestion.client_reply == make_suggestion().client_reply


def test_extract_json_keeps_plain_json():
    assert extract_json('  {"a": "{x}"}  ') == '{"a": "{x}"}'


def test_usage_variants():
    assert parse_usage(
        {"prompt_tokens": 100, "completion_tokens": 10, "prompt_cache_hit_tokens": 60}
    ) == Usage(input_tokens=40, output_tokens=10, cache_read_input_tokens=60)
    openrouter = {
        "prompt_tokens": 100,
        "completion_tokens": 10,
        "prompt_tokens_details": {"cached_tokens": 50, "cache_write_tokens": 30},
    }
    assert parse_usage(openrouter) == Usage(
        input_tokens=20, output_tokens=10, cache_read_input_tokens=50, cache_creation_input_tokens=30
    )
    assert parse_usage(None) == Usage()


# ---------- Ответ модели не годится ----------


@pytest.mark.parametrize(
    ("response", "error_type"),
    [
        (completion(GOOD, finish="length"), LLMTruncatedError),
        (completion("", finish="content_filter"), LLMRefusedError),
        (completion(None, refusal="Не могу помочь"), LLMRefusedError),
        (completion("   "), LLMBadOutputError),
        (completion('{"intent": "price"}'), LLMBadOutputError),
        (httpx.Response(200, json={"choices": []}), LLMBadOutputError),
    ],
)
async def test_bad_outputs(kb, tmp_path, response, error_type):
    with pytest.raises(error_type):
        await make_client(tmp_path, gateway=FakeGateway(response)).generate(make_call(kb))


async def test_failed_attempt_keeps_usage(kb, tmp_path):
    # Токены обрезанного ответа тоже оплачены — ядро суммирует их с повтором.
    with pytest.raises(LLMTruncatedError) as info:
        await make_client(tmp_path, gateway=FakeGateway(completion(GOOD, finish="length"))).generate(
            make_call(kb)
        )
    assert info.value.usage == Usage(input_tokens=1000, output_tokens=700, cache_read_input_tokens=4000)


async def test_error_inside_200_body(kb, tmp_path):
    gateway = FakeGateway(httpx.Response(200, json={"error": {"message": "upstream timeout"}}))
    with pytest.raises(LLMUnavailableError, match="upstream timeout"):
        await make_client(tmp_path, gateway=gateway).generate(make_call(kb))


# ---------- Упрощение запроса ----------


async def test_rejected_params_downgrade_and_are_remembered(kb, tmp_path):
    gateway = FakeGateway(
        error(400, "Unknown parameter: reasoning_effort"),
        error(400, "response_format json_schema is not supported"),
        completion(GOOD),
    )
    client = make_client(tmp_path, "kimi", gateway=gateway)
    await client.generate(make_call(kb))
    first, second, third = gateway.bodies
    assert first["reasoning_effort"] == "low" and first["response_format"]["type"] == "json_schema"
    assert "reasoning_effort" not in second and second["response_format"]["type"] == "json_schema"
    assert third["response_format"] == {"type": "json_object"}

    # Следующий запрос сразу идёт в рабочем варианте.
    await client.generate(make_call(kb))
    assert len(gateway.requests) == 4
    assert gateway.bodies[3]["response_format"] == {"type": "json_object"}


async def test_param_error_wrapped_in_5xx_also_downgrades(kb, tmp_path):
    gateway = FakeGateway(error(500, "upstream: invalid value for enable_thinking"), completion(GOOD))
    await make_client(tmp_path, "qwen", gateway=gateway).generate(make_call(kb))
    assert "enable_thinking" not in gateway.bodies[1]


async def test_all_variants_rejected(kb, tmp_path):
    gateway = FakeGateway(error(400, "bad request"))
    with pytest.raises(LLMUnavailableError, match="не принял запрос"):
        await make_client(tmp_path, "glm", gateway=gateway).generate(make_call(kb))
    assert len(gateway.requests) == 3


# ---------- Ошибки шлюза ----------


async def test_auth_error_is_not_retried(kb, tmp_path):
    gateway = FakeGateway(error(401, "Invalid token"))
    with pytest.raises(LLMUnavailableError, match="LLM_GATEWAY_KEY"):
        await make_client(tmp_path, gateway=gateway).generate(make_call(kb))
    assert len(gateway.requests) == 1


async def test_unknown_model_points_to_setting(kb, tmp_path):
    gateway = FakeGateway(
        error(503, "no available channel for model kimi-k3 under group default", "model_not_found")
    )
    with pytest.raises(LLMUnavailableError, match="LLM_GATEWAY_MODEL_KIMI"):
        await make_client(tmp_path, gateway=gateway).generate(make_call(kb))
    assert len(gateway.requests) == 1


async def test_wrong_url_hint(kb, tmp_path):
    gateway = FakeGateway(httpx.Response(404, text="Not Found"))
    with pytest.raises(LLMUnavailableError, match="/v1"):
        await make_client(tmp_path, gateway=gateway).generate(make_call(kb))


async def test_no_funds(kb, tmp_path):
    with pytest.raises(LLMUnavailableError, match="не хватает средств"):
        await make_client(tmp_path, gateway=FakeGateway(error(402, "Insufficient credits"))).generate(
            make_call(kb)
        )


async def test_transient_errors_are_retried(kb, tmp_path):
    gateway = FakeGateway(
        error(429, "rate limited"), httpx.Response(502, text="Bad Gateway"), completion(GOOD)
    )
    response = await make_client(tmp_path, gateway=gateway).generate(make_call(kb))
    assert response.suggestion == make_suggestion()
    assert len(gateway.requests) == 3


async def test_network_errors_give_up_after_retries(kb, tmp_path):
    gateway = FakeGateway(httpx.ConnectError("tunnel is down"))
    with pytest.raises(LLMUnavailableError, match="tunnel is down"):
        await make_client(tmp_path, gateway=gateway).generate(make_call(kb))
    assert len(gateway.requests) == 3


async def test_not_configured(kb, tmp_path):
    gateway = FakeGateway(completion(GOOD))
    client = make_client(tmp_path, gateway=gateway, llm_gateway_key=None)
    assert client.problem == NO_GATEWAY
    with pytest.raises(LLMUnavailableError, match="LLM_GATEWAY_URL"):
        await client.generate(make_call(kb))
    assert gateway.requests == []


async def test_read_timeout_is_not_retried(kb, tmp_path):
    gateway = FakeGateway(httpx.ReadTimeout("slow model"))
    with pytest.raises(LLMUnavailableError, match="LLM_TIMEOUT_SECONDS"):
        await make_client(tmp_path, gateway=gateway).generate(make_call(kb))
    assert len(gateway.requests) == 1  # повтор снова упёрся бы в таймаут, а платить пришлось бы дважды


async def test_cloudflare_524_is_not_retried(kb, tmp_path):
    gateway = FakeGateway(httpx.Response(524, text="A timeout occurred"))
    with pytest.raises(LLMUnavailableError, match="100 с"):
        await make_client(tmp_path, gateway=gateway).generate(make_call(kb))
    assert len(gateway.requests) == 1


async def test_upstream_404_is_retried(kb, tmp_path):
    # Адрес верный (запросы проходят), а канал за шлюзом разово ответил 404.
    gateway = FakeGateway(
        error(404, "upstream error: status code 404", "bad_response_status_code"), completion(GOOD)
    )
    response = await make_client(tmp_path, gateway=gateway).generate(make_call(kb))
    assert response.suggestion == make_suggestion() and len(gateway.requests) == 2


async def test_new_api_invalid_url(kb, tmp_path):
    gateway = FakeGateway(error(404, "Invalid URL (POST /chat/completions)"))
    with pytest.raises(LLMUnavailableError, match=r"Invalid URL.*LLM_GATEWAY_URL"):
        await make_client(tmp_path, gateway=gateway).generate(make_call(kb))
    assert len(gateway.requests) == 1


CLOUDFLARE_530 = (
    '<!doctype html>\n<!--[if lt IE 7]> <html class="no-js ie6 oldie" lang="en-US"> <![endif]-->\n'
    "<html><head><title>Origin DNS error | gw.trycloudflare.com | Cloudflare</title></head>"
    "<body>…</body></html>"
)


async def test_tunnel_down_message(kb, tmp_path):
    gateway = FakeGateway(httpx.Response(530, text=CLOUDFLARE_530, headers={"content-type": "text/html"}))
    with pytest.raises(LLMUnavailableError) as info:
        await make_client(tmp_path, gateway=gateway).generate(make_call(kb))
    message = str(info.value)
    assert "Туннель до шлюза не подключён" in message and "Origin DNS error" in message
    assert "<!doctype" not in message and "setup_gateway" in message
    assert len(gateway.requests) == 3  # именованный туннель может переподключиться — повторяем


async def test_invalid_url_is_reported(kb, tmp_path):
    client = make_client(tmp_path, llm_gateway_url="https://<новый-адрес>.trycloudflare.com/v1")
    with pytest.raises(LLMUnavailableError, match="Некорректный LLM_GATEWAY_URL"):
        await client.generate(make_call(kb))


DENIED = "Access denied, please make sure your account is in good standing."


async def test_access_denied_only_for_some_params(kb, tmp_path):
    # Так вёл себя Qwen за шлюзом заказчика: с response_format — отказ, без него — ответ.
    gateway = FakeGateway(error(400, DENIED), error(400, DENIED), error(400, DENIED), completion(GOOD))
    client = make_client(tmp_path, "qwen", gateway=gateway)
    response = await client.generate(make_call(kb))
    assert response.suggestion == make_suggestion()
    assert "response_format" not in gateway.bodies[-1]


async def test_access_denied_everywhere(kb, tmp_path):
    gateway = FakeGateway(error(400, DENIED))
    with pytest.raises(LLMUnavailableError, match="отказал шлюзу в доступе"):
        await make_client(tmp_path, "qwen", gateway=gateway).generate(make_call(kb))
    assert len(gateway.requests) == 4  # все варианты запроса; отказы приходят сразу и бесплатны
