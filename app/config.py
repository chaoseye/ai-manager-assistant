"""Настройки приложения из переменных окружения и файла .env."""

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent

# На Vercel (системная переменная VERCEL=1) файловая система доступна на запись только в /tmp,
# а экземпляр функции «засыпает» между запросами — фоновый цикл обработчика там ненадёжен.
ON_VERCEL = bool(os.environ.get("VERCEL"))

Effort = Literal["low", "medium", "high", "xhigh", "max"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # LLM
    llm_mode: Literal["live", "mock"] = "mock"
    llm_model: str = "claude-opus-5-5"
    llm_effort: Effort = "medium"
    llm_max_tokens: int = 8000
    llm_timeout_seconds: float = 60.0
    llm_fallbacks: bool = True
    anthropic_api_key: str | None = None

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

    @field_validator("kb_dir", "mock_llm_dir", "scenarios_dir", "db_path", "amocrm_mock_seed", mode="after")
    @classmethod
    def _resolve_path(cls, value: Path) -> Path:
        # Относительные пути считаются от корня проекта, а не от текущего каталога:
        # так CLI и сервис работают одинаково, откуда бы их ни запустили.
        return value if value.is_absolute() else (BASE_DIR / value).resolve()

    @property
    def amocrm_api_base(self) -> str:
        if self.amocrm_base_url:
            return self.amocrm_base_url.rstrip("/")
        if self.amocrm_subdomain:
            return f"https://{self.amocrm_subdomain}.amocrm.ru"
        return "https://mock.amocrm.ru"

    def amocrm_config_errors(self) -> list[str]:
        """Чего не хватает для выбранного AMOCRM_MODE. Проверяется при старте сервиса."""
        if self.amocrm_mode == "off":
            return []
        errors: list[str] = []
        if not self.webhook_secret:
            errors.append("WEBHOOK_SECRET не задан: без него вебхуки amoCRM не принимаются")
        if self.amocrm_mode == "live":
            if not (self.amocrm_subdomain or self.amocrm_base_url):
                errors.append("AMOCRM_SUBDOMAIN (или AMOCRM_BASE_URL) не задан")
            if not self.amocrm_token:
                errors.append("AMOCRM_TOKEN не задан: нужен долгосрочный токен приватной интеграции")
        return errors


@lru_cache
def get_settings() -> Settings:
    return Settings()
