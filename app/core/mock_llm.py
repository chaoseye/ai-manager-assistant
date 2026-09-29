"""LLM без модели: записанные ответы для демо-сценариев и простой поиск по FAQ.

Нужен, чтобы демо и тесты работали без API-ключа. Качество ответов здесь не показатель —
его проверяет eval-прогон на реальной модели.
"""

import json
import logging
import re
from pathlib import Path

from pydantic import BaseModel, ValidationError

from app.core.llm import LLMCall, LLMResponse
from app.core.schemas import Suggestion, Upsell, Usage
from app.kb.models import KnowledgeBase

logger = logging.getLogger(__name__)

# Похожесть = доля совпавших основ в вопросе FAQ × доля совпавших основ в сообщении.
# Учитываем обе стороны, иначе длинная жалоба «кондиционер после установки гудит» совпадёт
# с коротким вопросом «установите мой кондиционер».
FAQ_MATCH_THRESHOLD = 0.4
_STOP_WORDS = {"как", "что", "или", "для", "это", "вас", "нас", "мне", "меня", "можно", "есть", "при", "ваш"}


class Recording(BaseModel):
    id: str
    match: str
    suggestion: Suggestion


def normalize(text: str) -> str:
    text = text.lower().replace("ё", "е")
    return " ".join(re.findall(r"[\w]+", text))


def _stems(text: str) -> set[str]:
    words = re.findall(r"[а-яa-z0-9]+", text.lower().replace("ё", "е"))
    return {word[:5] for word in words if len(word) >= 3 and word not in _STOP_WORDS}


def load_recordings(directory: Path) -> dict[str, Recording]:
    recordings: dict[str, Recording] = {}
    if not directory.is_dir():
        return recordings
    for path in sorted(directory.glob("*.json")):
        try:
            recording = Recording.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except (ValueError, ValidationError) as exc:
            raise ValueError(f"Некорректная запись mock-LLM {path.name}: {exc}") from exc
        recordings[normalize(recording.match)] = recording
    return recordings


def _no_upsell(reason: str) -> Upsell:
    return Upsell(
        recommended=False, timing="not_now", product_ids=[], offer="", reason=reason, pitch="", avoid=""
    )


def faq_fallback(message: str, kb: KnowledgeBase, contact_name: str | None) -> Suggestion:
    """Ответ по самому похожему вопросу FAQ; если такого нет — «уточню» и needs_human."""
    message_stems = _stems(message)
    best_score, best_item = 0.0, None
    for item in kb.faq:
        for question in item.questions:
            question_stems = _stems(question)
            if not question_stems:
                continue
            common = len(question_stems & message_stems)
            score = (common / len(question_stems)) * (common / len(message_stems)) if message_stems else 0.0
            if score > best_score:
                best_score, best_item = score, item

    prefix = f"{contact_name}, " if contact_name else ""
    no_upsell = _no_upsell("Демо-режим без модели: подсказка по допродаже не формируется.")
    if best_item is not None and best_score >= FAQ_MATCH_THRESHOLD:
        answer = best_item.answer
        if prefix:
            answer = answer[0].lower() + answer[1:]
        return Suggestion(
            intent="other",
            sentiment="neutral",
            client_reply=f"{prefix}{answer} Подсказать что-нибудь ещё?",
            kb_refs=[best_item.id],
            answer_found_in_kb=True,
            needs_human=False,
            needs_human_reason="",
            upsell=no_upsell,
        )
    return Suggestion(
        intent="other",
        sentiment="neutral",
        client_reply=f"{prefix}спасибо за вопрос! Уточню детали и вернусь с ответом в ближайшее время."
        if prefix
        else "Спасибо за вопрос! Уточню детали и вернусь с ответом в ближайшее время.",
        kb_refs=[],
        answer_found_in_kb=False,
        needs_human=True,
        needs_human_reason=(
            "Демо-режим без модели: для этого сообщения нет записанного ответа и подходящего вопроса в FAQ."
        ),
        upsell=no_upsell,
    )


class MockLLMClient:
    mode = "mock"
    model = "mock"
    problem = None

    def __init__(self, recordings_dir: Path):
        self._recordings = load_recordings(recordings_dir)
        logger.info("mock_llm_ready", extra={"fields": {"recordings": len(self._recordings)}})

    async def generate(self, call: LLMCall) -> LLMResponse:
        recording = self._recordings.get(normalize(call.request.message))
        if recording is not None:
            suggestion = recording.suggestion.model_copy(deep=True)
        else:
            name = call.request.lead.contact_name if call.request.lead else None
            suggestion = faq_fallback(call.request.message, call.kb, name)
        return LLMResponse(suggestion=suggestion, model=self.model, usage=Usage())
