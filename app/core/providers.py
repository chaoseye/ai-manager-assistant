"""Модели, между которыми можно переключаться, и их особенности в OpenAI-совместимом API.

Особенности сверены с документацией провайдеров на 29.09.2026:
- строгая JSON-схема ответа (response_format json_schema): Kimi K3, Qwen3.8-Max (только без рассуждений),
  Grok 4.7; у GLM и DeepSeek задокументирован только json_object;
- рассуждения: GLM-5.3 и Kimi K3 рассуждают всегда (reasoning_effort: low / high / max), DeepSeek V4 Pro —
  по умолчанию (low / high / max), Grok 4.7 — low / medium / high / xhigh; у Qwen рассуждения выключаем
  (enable_thinking=false), иначе JSON по схеме не гарантирован.
Claude через шлюз идёт без reasoning_effort: шлюзы переводят его в формат Anthropic по-разному.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal

from app.config import PROVIDER_IDS

EFFORT_ORDER = ("low", "medium", "high", "xhigh", "max")


@dataclass(frozen=True)
class ProviderSpec:
    id: str
    name: str  # как показывать в интерфейсе
    output: Literal["json_schema", "json_object"]  # какой response_format просить в первую очередь
    efforts: tuple[str, ...] = ()  # уровни reasoning_effort по возрастанию; пусто — не передавать
    extra_body: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))


PROVIDERS: dict[str, ProviderSpec] = {
    spec.id: spec
    for spec in (
        ProviderSpec("claude", "Claude", "json_schema"),
        ProviderSpec("glm", "GLM", "json_object", ("low", "high", "max")),
        ProviderSpec("deepseek", "DeepSeek", "json_object", ("low", "high", "max")),
        ProviderSpec("kimi", "Kimi", "json_schema", ("low", "high", "max")),
        ProviderSpec("qwen", "Qwen", "json_schema", extra_body=MappingProxyType({"enable_thinking": False})),
        ProviderSpec("grok", "Grok", "json_schema", ("low", "medium", "high", "xhigh")),
    )
}
assert tuple(PROVIDERS) == PROVIDER_IDS, "таблица провайдеров расходится с ProviderId в config.py"


def map_effort(requested: str, supported: tuple[str, ...]) -> str | None:
    """Ближайший уровень, который понимает модель, не выше запрошенного (или самый низкий из доступных).

    LLM_EFFORT=medium → low у GLM, DeepSeek и Kimi, medium у Grok; max → xhigh у Grok.
    """
    if not supported:
        return None
    rank = EFFORT_ORDER.index(requested)
    fitting = [level for level in supported if EFFORT_ORDER.index(level) <= rank]
    return fitting[-1] if fitting else supported[0]
