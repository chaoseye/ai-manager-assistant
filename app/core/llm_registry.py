"""Реестр моделей: какой клиент отвечает за какую модель и в каком порядке пробовать запасные.

- LLM_MODE=mock — на все модели отвечает MockLLMClient (записанные ответы и поиск по FAQ);
- Claude — через Anthropic API, если задан ANTHROPIC_API_KEY или шлюз не настроен; иначе через шлюз;
- GLM, DeepSeek, Kimi, Qwen, Grok — через OpenAI-совместимый шлюз (LLM_GATEWAY_URL, LLM_GATEWAY_KEY).
"""

import logging
from collections.abc import Sequence
from typing import Any

import httpx

from app.config import PROVIDER_IDS, Settings
from app.core.gateway_llm import GatewayLLMClient, list_gateway_models
from app.core.llm import AnthropicLLMClient, LLMClient
from app.core.mock_llm import MockLLMClient
from app.core.providers import PROVIDERS

logger = logging.getLogger(__name__)
GATEWAY_CHECK_TIMEOUT = 10.0


class LLMRegistry:
    def __init__(self, clients: dict[str, LLMClient], default: str, fallbacks: Sequence[str] = ()):
        if default not in clients:
            raise ValueError(f"модели по умолчанию «{default}» нет в реестре")
        self._clients = dict(clients)
        self.default = default
        # Запасные — по порядку, без повторов и без самой модели по умолчанию.
        self.fallbacks = [p for p in dict.fromkeys(fallbacks) if p != default and p in clients]
        # Результат проверки шлюза: None — не проверяли, "ok" или текст ошибки.
        self.gateway_check: str | None = None

    async def check_gateway(
        self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        """Спрашивает у шлюза, какие модели доступны ключу (бесплатно), и отмечает недоступными
        остальные: их не будет в списке на странице, запасные их пропустят, eval — тоже.
        Если шлюз не ответил, ничего не меняем: модели проверятся при запросе."""
        clients = [c for c in self._clients.values() if isinstance(c, GatewayLLMClient) and c.configured]
        if not clients or not settings.llm_gateway_url or not settings.llm_gateway_key:
            return
        try:
            available = await list_gateway_models(
                settings.llm_gateway_url,
                settings.llm_gateway_key,
                transport=transport,
                timeout=GATEWAY_CHECK_TIMEOUT,
            )
        except RuntimeError as exc:
            self.gateway_check = f"не выполнена: {exc}"
            logger.warning("gateway_check_failed", extra={"fields": {"error": str(exc)}})
            return
        for client in clients:
            client.apply_model_list(available)
        self.gateway_check = "ok"
        missing = [c.model for c in clients if c.problem]
        if missing:
            logger.warning("gateway_models_missing", extra={"fields": {"models": missing}})

    @property
    def default_client(self) -> LLMClient:
        return self._clients[self.default]

    def ids(self) -> list[str]:
        return list(self._clients)

    def get(self, provider: str) -> LLMClient:
        return self._clients[provider]

    def chain(self, provider: str | None) -> list[tuple[str, LLMClient]]:
        """Кого спрашивать по порядку. Если модель выбрана явно — только её: при сравнении моделей
        подмена ответа другой моделью запутала бы. Иначе — модель по умолчанию и запасные."""
        if provider is not None:
            return [(provider, self._clients[provider])]
        return [(p, self._clients[p]) for p in [self.default, *self.fallbacks]]

    def label(self, provider: str) -> str:
        spec = PROVIDERS.get(provider)
        model = self._clients[provider].model
        return f"{spec.name} ({model})" if spec else model

    def describe(self) -> list[dict[str, Any]]:
        """Список моделей для API и демо-страницы."""
        items = []
        for provider, client in self._clients.items():
            spec = PROVIDERS.get(provider)
            items.append(
                {
                    "id": provider,
                    "name": spec.name if spec else provider,
                    "model": client.model,
                    "route": getattr(client, "route", client.mode),
                    "available": client.problem is None,
                    "problem": client.problem,
                    "default": provider == self.default,
                }
            )
        return items


def build_llms(settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None) -> LLMRegistry:
    """transport подменяет HTTP шлюза в тестах."""
    if settings.llm_mode == "mock":
        mock = MockLLMClient(settings.mock_llm_dir)
        return LLMRegistry({p: mock for p in PROVIDER_IDS}, settings.llm_provider)

    clients: dict[str, LLMClient] = {}
    for provider, spec in PROVIDERS.items():
        if provider == "claude" and (settings.anthropic_api_key or not settings.gateway_configured):
            clients[provider] = AnthropicLLMClient(settings)
        else:
            clients[provider] = GatewayLLMClient(settings, spec, transport=transport)
    return LLMRegistry(clients, settings.llm_provider, settings.llm_fallback_providers)
