"""Настройки приложения из переменных окружения и файла .env."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent

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
    db_path: Path = Path("data/app.db")
    log_level: str = "INFO"
    log_texts: bool = False

    # amoCRM (этап 2)
    amocrm_mode: Literal["off", "mock", "live"] = "off"

    @field_validator("anthropic_api_key", "api_token", "admin_token", mode="before")
    @classmethod
    def _empty_to_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("kb_dir", "mock_llm_dir", "scenarios_dir", "db_path", mode="after")
    @classmethod
    def _resolve_path(cls, value: Path) -> Path:
        # Относительные пути считаются от корня проекта, а не от текущего каталога:
        # так CLI и сервис работают одинаково, откуда бы их ни запустили.
        return value if value.is_absolute() else (BASE_DIR / value).resolve()


@lru_cache
def get_settings() -> Settings:
    return Settings()
