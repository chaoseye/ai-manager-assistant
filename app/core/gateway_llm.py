"""LLM через шлюз (агрегатор) с OpenAI-совместимым API: POST {LLM_GATEWAY_URL}/chat/completions.

Подходит для New API, OpenRouter, LiteLLM и API самих провайдеров. Один клиент — одна модель,
её особенности описаны в ProviderSpec (app/core/providers.py).

Шлюз может не пропустить к модели часть параметров: это зависит от того, как у него настроен канал.
Поэтому на ответ 400 клиент упрощает запрос — сначала без параметров рассуждений, потом json_object
вместо JSON-схемы, потом без response_format — и запоминает вариант, который сработал. Схема ответа
всегда есть и в системном промпте, так что формат модель знает в любом случае, а результат проверяет
та же Pydantic-модель Suggestion, что и для Claude.
"""

import asyncio
import copy
import json
import logging
import re
from functools import lru_cache
from typing import Any

import httpx
from pydantic import ValidationError

from app.config import Settings
from app.core.llm import (
    LLMBadOutputError,
    LLMCall,
    LLMRefusedError,
    LLMResponse,
    LLMTruncatedError,
    LLMUnavailableError,
)
from app.core.providers import ProviderSpec, map_effort
from app.core.schemas import Suggestion, Usage

logger = logging.getLogger(__name__)

NO_GATEWAY = (
    "Шлюз LLM не настроен: задайте LLM_GATEWAY_URL и LLM_GATEWAY_KEY "
    "(команда `python -m app.setup_gateway --url https://<шлюз>/v1`), либо включите LLM_MODE=mock"
)
# 524 не повторяем: это Cloudflare не дождался ответа за 100 с, повтор снова упрётся в тот же предел.
RETRY_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 529, 530})
# Ошибка про параметры запроса, даже если шлюз завернул её в 5xx.
_PARAM_ERROR_RE = re.compile(
    r"response_format|json_schema|json_object|reasoning_effort|enable_thinking", re.IGNORECASE
)
_BARE_NOT_FOUND = frozenset({"not found", "404 not found", "404 page not found"})
# Отказ провайдера в доступе (Alibaba: «Access denied, please make sure your account is in good standing»).
# Варианты запроса при нём всё равно перебираем: отказ бывает только для части параметров
# (так было с Qwen: с response_format — отказ, без него — ответ), а отказ приходит сразу и бесплатно.
_ACCESS_ERROR_RE = re.compile(
    r"access denied|good standing|arrearage|overdue|insufficient[_ ]quota"
    r"|account (?:is )?(?:suspended|disabled)",
    re.IGNORECASE,
)
_HTML_TITLE_RE = re.compile(r"<title>\s*(.*?)\s*</title>", re.S | re.I)
# Cloudflare: 530 и 1033 — туннель не подключён, 502 — туннель есть, а шлюз за ним не отвечает.
TUNNEL_DOWN_STATUSES = frozenset({530})
_THINK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)
_FENCE_RE = re.compile(r"^```[a-zA-Z]*\s*(.*?)\s*```$", re.S)

FORMAT_TEMPLATE = """\
<response_format>
Верни ответ одним json-объектом: без пояснений до и после, без Markdown и без блока ```.
JSON-схема ответа:
{schema}
Пример структуры (значения показывают только форму):
{example}
</response_format>"""

_EXAMPLE = {
    "intent": "price",
    "sentiment": "neutral",
    "client_reply": "…",
    "kb_refs": ["id записи базы знаний"],
    "answer_found_in_kb": True,
    "needs_human": False,
    "needs_human_reason": "",
    "upsell": {
        "recommended": False,
        "timing": "not_now",
        "product_ids": [],
        "offer": "",
        "reason": "…",
        "pitch": "",
        "avoid": "",
    },
}


def _strict(node: Any, defs: dict[str, Any]) -> Any:
    if isinstance(node, list):
        return [_strict(item, defs) for item in node]
    if not isinstance(node, dict):
        return node
    if "$ref" in node:
        return _strict(copy.deepcopy(defs[node["$ref"].rsplit("/", 1)[-1]]), defs)
    result: dict[str, Any] = {}
    for key, value in node.items():
        if key == "title":
            continue
        if key == "properties":
            result[key] = {name: _strict(sub, defs) for name, sub in value.items()}
        else:
            result[key] = _strict(value, defs)
    if result.get("type") == "object":
        result["additionalProperties"] = False
        result["required"] = list(result.get("properties", {}))
    return result


@lru_cache(maxsize=1)
def response_schema() -> dict[str, Any]:
    """JSON-схема Suggestion для strict structured outputs: без $ref и title, все поля обязательны,
    лишние поля запрещены."""
    schema = Suggestion.model_json_schema()
    defs = schema.pop("$defs", {})
    return _strict(schema, defs)


@lru_cache(maxsize=1)
def format_instruction() -> str:
    return FORMAT_TEMPLATE.format(
        schema=json.dumps(response_schema(), ensure_ascii=False),
        example=json.dumps(_EXAMPLE, ensure_ascii=False),
    )


_JSON_DECODER = json.JSONDecoder()
_MAX_JSON_STARTS = 200  # сколько «{» пробовать: без предела «{{{…» в ответе разбирается за O(n²)


def extract_json(text: str) -> str:
    """JSON-объект из ответа модели: без <think>…</think>, без ```-обёртки и текста вокруг.

    Объект ищется разбором с каждой «{», а не между первой «{» и последней «}»: иначе ломают и «:}» после
    JSON, и скобки в незакрытом <think>. Из нескольких объектов берём ответ помощника (с client_reply),
    иначе самый длинный. Если не разобрался ни один — прежняя вырезка, ошибку покажет валидация.
    """
    text = _THINK_RE.sub("", text).strip()
    fence = _FENCE_RE.match(text)
    if fence:
        text = fence.group(1).strip()
    candidates: list[tuple[bool, int, str]] = []
    position = text.find("{")
    for _ in range(_MAX_JSON_STARTS):
        if position == -1:
            break
        try:
            value, end = _JSON_DECODER.raw_decode(text, position)
        except ValueError:
            position = text.find("{", position + 1)
            continue
        if isinstance(value, dict):
            candidates.append(("client_reply" in value, end - position, text[position:end]))
        position = text.find("{", end)  # вложенные объекты найденного не разбираем отдельно
    if candidates:
        return max(candidates, key=lambda c: (c[0], c[1]))[2]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        text = text[start : end + 1]
    return text


def _int(value: Any) -> int:
    return value if isinstance(value, int) and value > 0 else 0


def parse_usage(raw: Any) -> Usage:
    """usage в формате Chat Completions → Usage. input_tokens — без токенов из кэша, как у Anthropic."""
    if not isinstance(raw, dict):
        return Usage()
    details = raw.get("prompt_tokens_details") or {}
    cached = _int(details.get("cached_tokens")) or _int(raw.get("prompt_cache_hit_tokens"))  # DeepSeek
    cache_write = _int(details.get("cache_write_tokens"))  # OpenRouter
    return Usage(
        input_tokens=max(_int(raw.get("prompt_tokens")) - cached - cache_write, 0),
        output_tokens=_int(raw.get("completion_tokens")),
        cache_read_input_tokens=cached,
        cache_creation_input_tokens=cache_write,
    )


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # [{"type": "text", "text": "..."}]
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return ""


def _error_info(response: httpx.Response) -> tuple[str, str]:
    """(код, текст) ошибки шлюза: {"error": {"code", "message"}} у OpenAI-совместимых API."""
    try:
        data = response.json()
    except ValueError:
        text = response.text
        title = _HTML_TITLE_RE.search(text) if "<html" in text[:500].lower() else None
        if title:  # страница ошибки прокси (Cloudflare и т. п.) — достаточно её заголовка
            return "", " ".join(title.group(1).split())[:200]
        return "", text[:300].strip() or response.reason_phrase
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        return str(error.get("code") or ""), str(error.get("message") or error)[:500]
    if isinstance(data, dict) and data.get("message"):
        return str(data.get("code") or ""), str(data["message"])[:500]
    return "", str(data)[:300]


async def list_gateway_models(
    url: str, key: str, *, transport: httpx.AsyncBaseTransport | None = None, timeout: float = 20.0
) -> list[str]:
    """id моделей, доступных ключу (GET {url}/models): токены моделей не тратятся.
    RuntimeError с понятным текстом — если шлюз не ответил или не принял ключ."""
    try:
        async with httpx.AsyncClient(timeout=timeout, transport=transport) as http:
            response = await http.get(f"{url}/models", headers={"Authorization": f"Bearer {key}"})
    except httpx.InvalidURL as exc:
        raise RuntimeError(f"некорректный адрес шлюза «{url}»: {exc}") from exc
    except httpx.TransportError as exc:
        raise RuntimeError(f"шлюз недоступен: {type(exc).__name__}: {exc}") from exc
    if response.status_code in (401, 403):
        raise RuntimeError(f"шлюз не принял ключ (HTTP {response.status_code})")
    if response.status_code == 404:
        raise RuntimeError(f"по адресу {url}/models ничего нет — адрес шлюза обычно заканчивается на /v1")
    if response.status_code >= 300:
        raise RuntimeError(f"шлюз ответил HTTP {response.status_code}: {response.text[:200]}")
    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError("шлюз ответил не JSON — проверьте адрес") from exc
    items = data.get("data") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise RuntimeError("шлюз вернул список моделей в незнакомом формате")
    return [str(item.get("id")) for item in items if isinstance(item, dict) and item.get("id")]


class _ParamsRejected(Exception):
    """Шлюз не принял параметры запроса — можно попробовать упрощённый запрос."""


def request_levels(spec: ProviderSpec) -> list[tuple[str | None, bool]]:
    """Варианты запроса от полного к простому: (response_format, передавать ли параметры рассуждений)."""
    formats: list[str | None] = ["json_schema", "json_object", None]
    if spec.output == "json_object":
        formats = ["json_object", None]
    levels: list[tuple[str | None, bool]] = []
    if spec.efforts or spec.extra_body:
        levels.append((formats[0], True))
    levels += [(fmt, False) for fmt in formats]
    return levels


class GatewayLLMClient:
    """Модель через OpenAI-совместимый шлюз: Chat Completions без стриминга."""

    mode = "live"
    route = "gateway"

    def __init__(
        self,
        settings: Settings,
        spec: ProviderSpec,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        max_retries: int = 2,
        backoff_seconds: float = 1.0,
    ):
        self.provider = spec.id
        self.model = settings.gateway_model(spec.id)
        self._spec = spec
        self._base_url = settings.llm_gateway_url or ""
        self._key = settings.llm_gateway_key
        self._timeout = settings.llm_timeout_seconds
        # reasoning_effort для этой модели: LLM_EFFORT, приведённый к её уровням (None — не передаём)
        self.effort = map_effort(settings.llm_effort, spec.efforts)
        self._transport = transport
        self._max_retries = max_retries
        self._backoff = backoff_seconds
        self._levels = request_levels(spec)
        self._level = 0  # вариант запроса, который шлюз принял последним
        self._missing: str | None = None  # модели нет среди доступных ключу (см. apply_model_list)

    @property
    def configured(self) -> bool:
        return bool(self._base_url and self._key)

    @property
    def problem(self) -> str | None:
        return NO_GATEWAY if not self.configured else self._missing

    def apply_model_list(self, available: list[str]) -> None:
        """Отмечает модель недоступной, если её нет в списке моделей шлюза для этого ключа."""
        if self.model in available:
            self._missing = None
            return
        family = [m for m in available if self.provider in m.lower()]
        hint = f" Из этого семейства ключу доступны: {', '.join(family[:6])}." if family else ""
        self._missing = (
            f"Модели «{self.model}» нет в шлюзе для этого ключа.{hint} Откройте её ключу в настройках "
            f"шлюза или задайте другой id в LLM_GATEWAY_MODEL_{self.provider.upper()}"
        )

    def build_body(self, call: LLMCall, level: int = 0) -> dict[str, Any]:
        response_format, with_options = self._levels[level]
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                # Системный промпт одинаков для всех запросов этой модели — шлюзы и провайдеры
                # кэшируют такой префикс сами.
                {"role": "system", "content": f"{call.system}\n\n{format_instruction()}"},
                {"role": "user", "content": call.user},
            ],
            "max_tokens": call.max_tokens,
        }
        if response_format == "json_schema":
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "suggestion", "strict": True, "schema": response_schema()},
            }
        elif response_format == "json_object":
            body["response_format"] = {"type": "json_object"}
        if with_options:
            if self.effort:
                body["reasoning_effort"] = self.effort
            body.update(self._spec.extra_body)
        return body

    async def generate(self, call: LLMCall) -> LLMResponse:
        if self.problem:
            raise LLMUnavailableError(self.problem)
        level = self._level
        while True:
            try:
                data = await self._post(self.build_body(call, level))
                break
            except _ParamsRejected as exc:
                if level + 1 >= len(self._levels):
                    if _ACCESS_ERROR_RE.search(str(exc)):
                        raise LLMUnavailableError(
                            f"Провайдер модели {self.model} отказал шлюзу в доступе ({exc}). "
                            "Проверьте канал этой модели в шлюзе"
                        ) from exc
                    raise LLMUnavailableError(f"Шлюз не принял запрос к модели {self.model}: {exc}") from exc
                level += 1
                response_format, with_options = self._levels[level]
                logger.warning(
                    "gateway_downgrade",
                    extra={
                        "fields": {
                            "model": self.model,
                            "response_format": response_format or "none",
                            "reasoning_options": with_options,
                            "error": str(exc),
                        }
                    },
                )
        self._level = level
        return self._parse(data, call)

    async def _post(self, body: dict[str, Any]) -> Any:
        url = f"{self._base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {self._key}"}
        error = LLMUnavailableError("Шлюз LLM недоступен")
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as http:
            for attempt in range(self._max_retries + 1):
                delay = self._backoff * (2**attempt)
                try:
                    response = await http.post(url, json=body, headers=headers)
                except (httpx.ReadTimeout, httpx.WriteTimeout) as exc:
                    # Модель думает дольше таймаута. Повтор снова упрётся в него, а шлюз может
                    # списать оплату за оба запроса.
                    raise LLMUnavailableError(
                        f"Модель {self.model} не ответила за {self._timeout:.0f} с: "
                        "увеличьте LLM_TIMEOUT_SECONDS или снизьте LLM_EFFORT"
                    ) from exc
                except httpx.InvalidURL as exc:
                    raise LLMUnavailableError(
                        f"Некорректный LLM_GATEWAY_URL «{self._base_url}»: {exc}"
                    ) from exc
                except httpx.TransportError as exc:  # сеть: соединение, обрыв
                    error = LLMUnavailableError(f"Шлюз LLM недоступен: {type(exc).__name__}: {exc}")
                else:
                    status = response.status_code
                    if status < 300:
                        try:
                            return response.json()
                        except ValueError as exc:
                            raise LLMUnavailableError(f"Шлюз LLM вернул не JSON (HTTP {status})") from exc
                    code, detail = _error_info(response)
                    self._raise_for_status(status, code, detail)
                    error = LLMUnavailableError(f"Шлюз LLM вернул {status}: {detail}")
                    if status in TUNNEL_DOWN_STATUSES:
                        error = LLMUnavailableError(
                            f"Туннель до шлюза не подключён (HTTP {status}: {detail}). Перезапустите "
                            "туннель; если адрес сменился — python -m app.setup_gateway --url <новый адрес>"
                        )
                    retry_after = response.headers.get("retry-after", "")
                    if retry_after.isdigit():
                        delay = max(delay, min(float(retry_after), 30.0))
                if attempt < self._max_retries:
                    logger.warning(
                        "gateway_retry",
                        extra={"fields": {"model": self.model, "attempt": attempt + 1, "error": str(error)}},
                    )
                    await asyncio.sleep(delay)
        raise error

    def _raise_for_status(self, status: int, code: str, detail: str) -> None:
        """Ошибки, которые повторять бессмысленно. Для 429, 5xx и сетевых сбоев просто возвращается."""
        setting = f"LLM_GATEWAY_MODEL_{self.provider.upper()}"
        if status in (401, 403):
            raise LLMUnavailableError(
                f"Шлюз LLM отклонил ключ ({status}): {detail}. Проверьте LLM_GATEWAY_KEY"
            )
        if status == 402:
            raise LLMUnavailableError(f"На балансе шлюза LLM не хватает средств (402): {detail}")
        if status == 524:
            raise LLMUnavailableError(
                f"Шлюз не дождался ответа модели {self.model} за 100 с (предел Cloudflare-туннеля). "
                "Снизьте LLM_EFFORT или откройте шлюз без туннеля"
            )
        lowered = detail.lower()
        # New API: код model_not_found / «no available channel»; OpenRouter: «not a valid model ID».
        if code == "model_not_found" or "no available channel" in lowered or "not a valid model" in lowered:
            raise LLMUnavailableError(f"В шлюзе нет модели «{self.model}»: {detail}. Проверьте {setting}")
        if status in (400, 422) or _PARAM_ERROR_RE.search(detail):
            raise _ParamsRejected(f"{status}: {detail}")
        if status == 404:
            # Неверный адрес: New API отвечает «Invalid URL (…)», веб-серверы — голым «Not Found».
            # Остальные 404 приходят от модели за шлюзом (канал ответил 404) — бывают разово, повторяем.
            if "invalid url" in lowered or lowered.strip() in _BARE_NOT_FOUND or "<html" in lowered:
                raise LLMUnavailableError(
                    f"Шлюз вернул 404 на {self._base_url}/chat/completions ({detail}): проверьте "
                    "LLM_GATEWAY_URL (обычно он заканчивается на /v1)"
                )
            return
        if status not in RETRY_STATUSES:
            raise LLMUnavailableError(f"Шлюз LLM вернул {status}: {detail}")

    def _parse(self, data: Any, call: LLMCall) -> LLMResponse:
        if not isinstance(data, dict):
            raise LLMBadOutputError("Шлюз вернул ответ не в формате Chat Completions")
        if data.get("error") and not data.get("choices"):
            error = data["error"]
            message = error.get("message") if isinstance(error, dict) else error
            raise LLMUnavailableError(f"Шлюз LLM вернул ошибку: {message}")
        usage = parse_usage(data.get("usage"))
        choices = data.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            raise LLMBadOutputError("В ответе шлюза нет choices", usage)
        choice = choices[0]
        message = choice.get("message") or {}
        finish = choice.get("finish_reason")

        # Причину остановки проверяем до чтения содержимого.
        if message.get("refusal"):
            raise LLMRefusedError(f"Модель отказалась отвечать: {message['refusal']}", usage)
        if finish == "content_filter":
            raise LLMRefusedError("Ответ модели остановлен фильтром содержимого", usage)
        if finish == "length":
            raise LLMTruncatedError(f"Ответ обрезан на {call.max_tokens} токенах", usage)

        text = _content_text(message.get("content"))
        if not text.strip():
            raise LLMBadOutputError("Модель вернула пустой ответ", usage)
        try:
            suggestion = Suggestion.model_validate_json(extract_json(text))
        except ValidationError as exc:
            raise LLMBadOutputError(f"Ответ модели не соответствует схеме: {exc}", usage) from exc
        return LLMResponse(suggestion=suggestion, model=str(data.get("model") or self.model), usage=usage)
