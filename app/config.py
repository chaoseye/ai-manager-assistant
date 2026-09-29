"""Настройки приложения из переменных окружения и файла .env."""

import os
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal, get_args

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent

# На Vercel (системная переменная VERCEL=1) файловая система доступна на запись только в /tmp,
# а экземпляр функции «засыпает» между запросами — фоновый цикл обработчика там ненадёжен.
ON_VERCEL = bool(os.environ.get("VERCEL"))

Effort = Literal["low", "medium", "high", "xhigh", "max"]
# Модели, между которыми можно переключаться. Особенности каждой — в app/core/providers.py.
ProviderId = Literal["claude", "glm", "deepseek", "kimi", "qwen", "grok"]
PROVIDER_IDS: tuple[str, ...] = get_args(ProviderId)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # LLM
    llm_mode: Literal["live", "mock"] = "mock"
    # Модель по умолчанию: для amoCRM и запросов, где модель не выбрана.
    llm_provider: ProviderId = "claude"
    # Запасные модели по порядку — если модель по умолчанию не ответила. В .env — через запятую.
    llm_fallback_providers: Annotated[list[ProviderId], NoDecode] = []
    llm_model: str = "claude-opus-5-5"  # Claude через Anthropic API
    llm_effort: Effort = "medium"
    llm_max_tokens: int = 8000
    llm_timeout_seconds: float = 60.0
    llm_fallbacks: bool = True  # серверный fallback Anthropic API
    anthropic_api_key: str | None = None

    # Шлюз (агрегатор) с OpenAI-совместимым API: New API, OpenRouter, LiteLLM…
    # Claude идёт через Anthropic API, если задан ANTHROPIC_API_KEY или шлюз не настроен; иначе — через шлюз.
    llm_gateway_url: str | None = None  # например, https://<шлюз>/v1
    llm_gateway_key: str | None = None
    # id моделей в шлюзе; по умолчанию — как в New API.
    llm_gateway_model_claude: str = "claude-opus-5"
    llm_gateway_model_glm: str = "glm-5.3"
    llm_gateway_model_deepseek: str = "deepseek-v4-pro"
    llm_gateway_model_kimi: str = "kimi-k3"
    llm_gateway_model_qwen: str = "qwen3.8-max"
    llm_gateway_model_grok: str = "grok-4.7"

    # Ядро
    kb_dir: Path = Path("knowledge_base")
    history_limit: int = 20
    pii_masking: bool = True
    upsell_in_reply: bool = False

    # Демо и mock-режим
    mock_llm_dir: Path = Path("examples/mock_llm")
    scenarios_dir: Path = Path("examples/scenarios")

    # Доступ к API
    api_token: str | None = None
    admin_token: str | None = None

    # Хранилище и логи
    db_path: Path = Path("/tmp/ai-manager/app.db") if ON_VERCEL else Path("data/app.db")
    log_level: str = "INFO"
    log_texts: bool = False

    # amoCRM (этап 2)
    amocrm_mode: Literal["off", "mock", "live"] = "off"
    amocrm_subdomain: str | None = None
    amocrm_base_url: str | None = None  # по умолчанию https://{subdomain}.amocrm.ru
    amocrm_account_id: int | None = None
    amocrm_token: str | None = None
    amocrm_rps: float = 5.0
    amocrm_timeout_seconds: float = 15.0
    amocrm_mock_seed: Path = Path("examples/amocrm/mock_account.json")
    webhook_secret: str | None = None
    note_service_name: str = "AI-помощник"

    # Очередь генераций (этап 2)
    worker_enabled: bool = not ON_VERCEL  # фоновый цикл обработчика
    # Обрабатывать созревшие задачи, когда страница «amoCRM (mock)» запрашивает ленту.
    # Нужно там, где фоновый цикл не работает (Vercel).
    worker_on_request: bool = ON_VERCEL
    worker_concurrency: int = 3
    worker_poll_seconds: float = 0.5
    debounce_seconds: float = 6.0
    on_manager_replied: Literal["upsell_only", "skip"] = "upsell_only"
    job_max_attempts: int = 3
    job_retry_seconds: float = 60.0
    retention_days: int = 30

    @field_validator(
        "anthropic_api_key",
        "llm_gateway_url",
        "llm_gateway_key",
        "api_token",
        "admin_token",
        "amocrm_subdomain",
        "amocrm_base_url",
        "amocrm_token",
        "webhook_secret",
        "amocrm_account_id",
        mode="before",
    )
    @classmethod
    def _empty_to_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("llm_fallback_providers", mode="before")
    @classmethod
    def _split_providers(cls, value: object) -> object:
        # LLM_FALLBACK_PROVIDERS=glm,qwen — список через запятую, пустая строка — без запасных.
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    @field_validator("llm_gateway_url", mode="after")
    @classmethod
    def _strip_slash(cls, value: str | None) -> str | None:
        return value.strip().rstrip("/") if value else value

    @field_validator("kb_dir", "mock_llm_dir", "scenarios_dir", "db_path", "amocrm_mock_seed", mode="after")
    @classmethod
    def _resolve_path(cls, value: Path) -> Path:
        # Относительные пути считаются от корня проекта, а не от текущего каталога:
        # так CLI и сервис работают одинаково, откуда бы их ни запустили.
        return value if value.is_absolute() else (BASE_DIR / value).resolve()

    @property
    def gateway_configured(self) -> bool:
        return bool(self.llm_gateway_url and self.llm_gateway_key)

    def gateway_model(self, provider: str) -> str:
        """id модели провайдера в шлюзе (LLM_GATEWAY_MODEL_<PROVIDER>)."""
        return getattr(self, f"llm_gateway_model_{provider}")

    @property
    def amocrm_api_base(self) -> str:
        if self.amocrm_base_url:
            return self.amocrm_base_url.rstrip("/")
        if self.amocrm_subdomain:
            return f"https://{self.amocrm_subdomain}.amocrm.ru"
        return "https://mock.amocrm.ru"

    def amocrm_config_errors(self) -> list[str]:
        """Чего не хватает для выбранного AMOCRM_MODE. Проверяется при старте сервиса.

        В mock-режиме WEBHOOK_SECRET необязателен: без него вебхук выключен (404), а страница
        «amoCRM (mock)» работает — она шлёт сообщения через /api/v1/amocrm-mock/messages.
        """
        if self.amocrm_mode != "live":
            return []
        errors: list[str] = []
        if not self.webhook_secret:
            errors.append("WEBHOOK_SECRET не задан: без него вебхуки amoCRM не принимаются")
        if not (self.amocrm_subdomain or self.amocrm_base_url):
            errors.append("AMOCRM_SUBDOMAIN (или AMOCRM_BASE_URL) не задан")
        if not self.amocrm_token:
            errors.append("AMOCRM_TOKEN не задан: нужен долгосрочный токен приватной интеграции")
        return errors


@lru_cache
def get_settings() -> Settings:
    return Settings()
