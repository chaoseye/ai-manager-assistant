"""Модели входа и выхода ядра."""

import unicodedata
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

Role = Literal["client", "manager", "bot"]
Mode = Literal["full", "upsell_only"]
Intent = Literal[
    "price", "availability", "delivery", "payment", "warranty", "complaint", "order_status", "other"
]
Sentiment = Literal["positive", "neutral", "negative"]
Timing = Literal["now", "after_resolution", "not_now"]


# ---------- Вход ----------

# Пробелы, переносы, управляющие и «форматирующие» символы (нулевой ширины, соединители, BOM): их не видно,
# и сообщение только из них — пустое, хотя strip() его не очищает.
_INVISIBLE_CATEGORIES = frozenset({"Cc", "Cf", "Zs", "Zl", "Zp"})


def has_visible_text(text: str) -> bool:
    return any(not ch.isspace() and unicodedata.category(ch) not in _INVISIBLE_CATEGORIES for ch in text)


class DialogMessage(BaseModel):
    role: Role
    text: str = Field(max_length=10_000)
    ts: datetime | None = None
    author_name: str | None = Field(default=None, max_length=200)


class LeadContext(BaseModel):
    id: int | None = None
    pipeline: str | None = Field(default=None, max_length=200)
    stage: str | None = Field(default=None, max_length=200)
    budget: int | None = Field(default=None, ge=0)
    products: list[str] = Field(default_factory=list, max_length=50)
    tags: list[str] = Field(default_factory=list, max_length=50)
    contact_name: str | None = Field(default=None, max_length=200)


class SuggestRequest(BaseModel):
    message: str = Field(max_length=10_000, description="Новое обращение клиента")
    history: list[DialogMessage] = Field(default_factory=list, max_length=500)
    replies: list[DialogMessage] = Field(
        default_factory=list,
        max_length=50,
        description="Реплики менеджера или бота, отправленные уже после нового обращения",
    )
    lead: LeadContext | None = None
    channel: str | None = Field(default=None, max_length=50)

    @field_validator("message")
    @classmethod
    def _message_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not has_visible_text(value):
            raise ValueError("Текст обращения пустой")
        return value


# ---------- Выход модели (JSON-схема для structured outputs) ----------


class Upsell(BaseModel):
    recommended: bool = Field(description="Стоит ли сейчас вообще что-то предлагать клиенту")
    timing: Timing = Field(
        description=(
            "now — предложить в этом же ответе; after_resolution — сначала решить вопрос клиента; "
            "not_now — не предлагать (жалоба, негатив, торг по основному товару, клиент уже отказался)"
        )
    )
    product_ids: list[str] = Field(description="id товаров и услуг из базы знаний (тип product)")
    offer: str = Field(description="Что предложить, коротко и по-человечески; пустая строка, если нечего")
    reason: str = Field(
        description="Почему именно этому клиенту: конкретные слова клиента, этап сделки, товары в сделке"
    )
    pitch: str = Field(
        description=(
            "Готовая фраза, которую менеджер может отправить клиенту; пустая строка, если предлагать не нужно"
        )
    )
    avoid: str = Field(description="Чего менеджеру не делать в этой ситуации; пустая строка, если нечего")


class Suggestion(BaseModel):
    intent: Intent = Field(description="Тема обращения")
    sentiment: Sentiment = Field(description="Настроение клиента")
    client_reply: str = Field(
        description="Черновик ответа клиенту, который менеджер отправит от своего имени"
    )
    kb_refs: list[str] = Field(description="id записей базы знаний, на которые опирается ответ клиенту")
    answer_found_in_kb: bool = Field(description="Нашёлся ли ответ на вопрос клиента в базе знаний")
    needs_human: bool = Field(description="Нужна ли проверка или решение менеджера перед отправкой")
    needs_human_reason: str = Field(description="Почему нужен человек; пустая строка, если не нужен")
    upsell: Upsell


# ---------- Результат ядра ----------


WarningField = Literal["client_reply", "upsell"]


class GuardWarning(BaseModel):
    code: str
    message: str
    # К какому блоку относится предупреждение и какой кусок его текста — по ним страница подсвечивает
    # место в черновике. None — предупреждение про результат в целом или без точного места.
    field: WarningField | None = None
    fragment: str | None = None


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_input_tokens=self.cache_read_input_tokens + other.cache_read_input_tokens,
            cache_creation_input_tokens=self.cache_creation_input_tokens + other.cache_creation_input_tokens,
        )


class Meta(BaseModel):
    suggestion_id: str
    mode: Mode
    llm_mode: str
    provider: str | None = None  # какая модель ответила: claude, glm, deepseek, kimi, qwen, grok
    model: str
    kb_version: str
    latency_ms: int
    attempts: int
    usage: Usage
    warnings: list[GuardWarning]
    created_at: datetime


class SuggestResult(BaseModel):
    suggestion: Suggestion
    meta: Meta
