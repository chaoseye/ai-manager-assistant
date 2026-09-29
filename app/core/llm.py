"""Клиент LLM: интерфейс, ошибки и реализация на официальном SDK Anthropic.

Другие модели идут через OpenAI-совместимый шлюз (app/core/gateway_llm.py); какой клиент отвечает
за какую модель, решает реестр (app/core/llm_registry.py).

SDK Anthropic импортируется лениво, при создании клиента для Claude: он тяжёлый (секунды на импорт),
а CLI, имитатор amoCRM, тесты в mock-режиме и работа только через шлюз без него обходятся.
"""

import logging
from dataclasses import dataclass, replace
from functools import lru_cache
from typing import Any, Protocol

from pydantic import ValidationError

from app.config import Settings
from app.core.schemas import Mode, Suggestion, SuggestRequest, Usage
from app.kb.models import KnowledgeBase

logger = logging.getLogger(__name__)

# Серверный fallback: если классификатор безопасности отклонит запрос, API сам повторит его
# на рекомендованной модели. Заголовок относится именно к форме fallbacks="default".
FALLBACK_BETA = "server-side-fallback-2026-07-01"


@lru_cache(maxsize=1)
def suggestion_schema() -> dict[str, Any]:
    """JSON-схема ответа модели, приведённая SDK к требованиям structured outputs."""
    from anthropic import transform_schema

    return transform_schema(Suggestion)


class LLMError(Exception):
    """Базовая ошибка генерации. usage — токены, потраченные на неудачную попытку (если известны)."""

    def __init__(self, message: str, usage: Usage | None = None):
        super().__init__(message)
        self.usage = usage or Usage()


class LLMUnavailableError(LLMError):
    """Сеть, лимиты, 5xx, ошибки авторизации или конфигурации — повторять позже."""


class LLMRefusedError(LLMError):
    """Модель (и fallback) отказались отвечать."""


class LLMTruncatedError(LLMError):
    """Ответ обрезан по max_tokens."""


class LLMBadOutputError(LLMError):
    """Ответ не соответствует схеме или пустой."""


@dataclass(frozen=True)
class LLMCall:
    system: str
    user: str
    request: SuggestRequest  # уже с замаскированными ПДн
    kb: KnowledgeBase
    mode: Mode
    max_tokens: int

    def with_max_tokens(self, max_tokens: int) -> "LLMCall":
        return replace(self, max_tokens=max_tokens)


@dataclass(frozen=True)
class LLMResponse:
    suggestion: Suggestion
    model: str
    usage: Usage


class LLMClient(Protocol):
    mode: str  # live или mock
    model: str

    @property
    def problem(self) -> str | None:
        """Почему клиент заведомо не сможет ответить (например, нет ключа); None — всё в порядке."""
        ...

    async def generate(self, call: LLMCall) -> LLMResponse: ...


NO_CREDENTIALS = (
    "Не найдены учётные данные Claude API: задайте ANTHROPIC_API_KEY в .env "
    "(или выполните `ant auth login`), либо включите LLM_MODE=mock"
)


class AnthropicLLMClient:
    """Claude через beta.messages.create: structured output + кэш системного промпта + fallback."""

    mode = "live"
    provider = "claude"
    route = "anthropic"

    def __init__(self, settings: Settings, client: Any | None = None):
        self._settings = settings
        self.model = settings.llm_model
        if client is None:
            import anthropic

            kwargs: dict[str, Any] = {"timeout": settings.llm_timeout_seconds, "max_retries": 2}
            if settings.anthropic_api_key:
                kwargs["api_key"] = settings.anthropic_api_key
            client = anthropic.AsyncAnthropic(**kwargs)
        self._client = client

    @property
    def problem(self) -> str | None:
        # SDK ищет ключ в api_key, auth_token или credentials (профиль `ant auth login`, федерация).
        if any(getattr(self._client, name, None) for name in ("api_key", "auth_token", "credentials")):
            return None
        return NO_CREDENTIALS

    def build_params(self, call: LLMCall) -> dict[str, Any]:
        params: dict[str, Any] = {
            "model": self._settings.llm_model,
            "max_tokens": call.max_tokens,
            # Системный промпт (правила + тон + вся БЗ) одинаков для всех запросов — кэшируем его.
            "system": [{"type": "text", "text": call.system, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": call.user}],
            "output_config": {
                "effort": self._settings.llm_effort,
                "format": {"type": "json_schema", "schema": suggestion_schema()},
            },
        }
        if self._settings.llm_fallbacks:
            params["betas"] = [FALLBACK_BETA]
            params["fallbacks"] = "default"
        return params

    async def generate(self, call: LLMCall) -> LLMResponse:
        import anthropic

        if self.problem:
            raise LLMUnavailableError(self.problem)
        params = self.build_params(call)
        try:
            response = await self._client.beta.messages.create(**params)
        except (anthropic.APIConnectionError, anthropic.RateLimitError, anthropic.InternalServerError) as exc:
            raise LLMUnavailableError(f"LLM временно недоступна: {exc}") from exc
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
            raise LLMUnavailableError(f"Нет доступа к Claude API: проверьте ключ ({exc})") from exc
        except anthropic.APIStatusError as exc:
            raise LLMUnavailableError(f"Claude API вернул ошибку {exc.status_code}: {exc}") from exc
        except anthropic.AnthropicError as exc:
            # Например, не найдены учётные данные.
            raise LLMUnavailableError(f"Ошибка клиента Claude API: {exc}") from exc

        usage = Usage(
            input_tokens=response.usage.input_tokens or 0,
            output_tokens=response.usage.output_tokens or 0,
            cache_read_input_tokens=response.usage.cache_read_input_tokens or 0,
            cache_creation_input_tokens=response.usage.cache_creation_input_tokens or 0,
        )

        # stop_reason проверяем до чтения содержимого.
        if response.stop_reason == "refusal":
            category = getattr(response.stop_details, "category", None) if response.stop_details else None
            raise LLMRefusedError(
                f"Модель отказалась отвечать (категория: {category or 'не указана'})", usage
            )
        if response.stop_reason == "max_tokens":
            raise LLMTruncatedError(f"Ответ обрезан на {call.max_tokens} токенах", usage)

        text = "".join(block.text for block in response.content if block.type == "text")
        try:
            suggestion = Suggestion.model_validate_json(text)
        except ValidationError as exc:
            raise LLMBadOutputError(f"Ответ модели не соответствует схеме: {exc}", usage) from exc

        return LLMResponse(suggestion=suggestion, model=response.model, usage=usage)
