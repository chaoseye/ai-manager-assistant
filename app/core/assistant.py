"""Ядро: обращение → контекст → LLM → проверки → два блока."""

import logging
import time
import uuid
from datetime import UTC, datetime

from app.config import Settings
from app.core.guards import apply_guards
from app.core.llm import LLMBadOutputError, LLMCall, LLMClient, LLMResponse, LLMTruncatedError
from app.core.pii import mask_pii
from app.core.prompts import build_system_prompt, build_user_prompt
from app.core.schemas import Meta, Mode, SuggestRequest, SuggestResult, Usage
from app.kb.loader import KnowledgeStore

logger = logging.getLogger(__name__)


class Assistant:
    def __init__(self, kb_store: KnowledgeStore, llm: LLMClient, settings: Settings):
        self.kb_store = kb_store
        self.llm = llm
        self.settings = settings
        self._system_prompts: dict[tuple[str, bool], str] = {}

    def prepare_request(self, request: SuggestRequest) -> SuggestRequest:
        """Обрезает историю до HISTORY_LIMIT и маскирует ПДн. Результат уходит в LLM и в хранилище."""
        history = request.history[-self.settings.history_limit :] if self.settings.history_limit > 0 else []
        prepared = request.model_copy(update={"history": list(history)}, deep=True)
        if self.settings.pii_masking:
            prepared.message = mask_pii(prepared.message)
            for message in prepared.history:
                message.text = mask_pii(message.text)
        return prepared

    def system_prompt(self) -> str:
        kb = self.kb_store.current
        key = (kb.version, self.settings.upsell_in_reply)
        if key not in self._system_prompts:
            self._system_prompts.clear()  # храним только текущую версию БЗ
            self._system_prompts[key] = build_system_prompt(kb, upsell_in_reply=self.settings.upsell_in_reply)
        return self._system_prompts[key]

    async def suggest(self, request: SuggestRequest, mode: Mode = "full") -> SuggestResult:
        started = time.perf_counter()
        kb = self.kb_store.current
        prepared = self.prepare_request(request)
        call = LLMCall(
            system=self.system_prompt(),
            user=build_user_prompt(prepared, mode=mode),
            request=prepared,
            kb=kb,
            mode=mode,
            max_tokens=self.settings.llm_max_tokens,
        )
        response, attempts, usage = await self._generate(call)
        suggestion, warnings = apply_guards(response.suggestion, kb, prepared, mode)

        meta = Meta(
            suggestion_id=uuid.uuid4().hex,
            mode=mode,
            llm_mode=self.llm.mode,
            model=response.model,
            kb_version=kb.version,
            latency_ms=round((time.perf_counter() - started) * 1000),
            attempts=attempts,
            usage=usage,
            warnings=warnings,
            created_at=datetime.now(UTC),
        )
        fields: dict[str, object] = {
            "suggestion_id": meta.suggestion_id,
            "mode": mode,
            "llm_mode": meta.llm_mode,
            "model": meta.model,
            "kb_version": meta.kb_version,
            "latency_ms": meta.latency_ms,
            "attempts": attempts,
            "usage": usage.model_dump(),
            "warnings": [w.code for w in warnings],
            "needs_human": suggestion.needs_human,
            "upsell_timing": suggestion.upsell.timing,
        }
        if self.settings.log_texts:
            fields["message"] = prepared.message
            fields["client_reply"] = suggestion.client_reply
        logger.info("suggestion_created", extra={"fields": fields})
        return SuggestResult(suggestion=suggestion, meta=meta)

    async def _generate(self, call: LLMCall) -> tuple[LLMResponse, int, Usage]:
        """Вызов LLM с повторами: обрезанный ответ — один раз с удвоенным лимитом;
        невалидный или пустой ответ — ещё одна попытка."""
        attempts = 0
        usage = Usage()
        truncated_retry = bad_output_retry = False
        while True:
            attempts += 1
            try:
                response = await self.llm.generate(call)
            except LLMTruncatedError as exc:
                usage = usage + exc.usage
                if truncated_retry:
                    raise
                truncated_retry = True
                call = call.with_max_tokens(call.max_tokens * 2)
                continue
            except LLMBadOutputError as exc:
                usage = usage + exc.usage
                if bad_output_retry:
                    raise
                bad_output_retry = True
                continue

            usage = usage + response.usage
            if call.mode == "full" and not response.suggestion.client_reply.strip():
                if bad_output_retry:
                    raise LLMBadOutputError("Модель вернула пустой ответ клиенту")
                bad_output_retry = True
                continue
            return response, attempts, usage
