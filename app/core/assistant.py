"""Ядро: обращение → контекст → LLM → проверки → два блока."""

import logging
import time
import uuid
from datetime import UTC, datetime

from app.config import Settings
from app.core.guards import apply_guards
from app.core.llm import LLMBadOutputError, LLMCall, LLMClient, LLMError, LLMResponse, LLMTruncatedError
from app.core.llm_registry import LLMRegistry
from app.core.pii import mask_pii
from app.core.prompts import build_system_prompt, build_user_prompt
from app.core.schemas import GuardWarning, Meta, Mode, SuggestRequest, SuggestResult, Usage
from app.kb.loader import KnowledgeStore

logger = logging.getLogger(__name__)


class Assistant:
    def __init__(self, kb_store: KnowledgeStore, llm: LLMRegistry | LLMClient, settings: Settings):
        """llm — реестр моделей или один клиент (тогда он отвечает за модель LLM_PROVIDER)."""
        self.kb_store = kb_store
        if not isinstance(llm, LLMRegistry):
            llm = LLMRegistry({settings.llm_provider: llm}, settings.llm_provider)
        self.llms = llm
        self.settings = settings
        self._system_prompts: dict[tuple[str, bool], str] = {}

    @property
    def llm(self) -> LLMClient:
        """Клиент модели по умолчанию."""
        return self.llms.default_client

    def prepare_request(self, request: SuggestRequest) -> SuggestRequest:
        """Обрезает историю до HISTORY_LIMIT и маскирует ПДн. Результат уходит в LLM и в хранилище."""
        history = request.history[-self.settings.history_limit :] if self.settings.history_limit > 0 else []
        prepared = request.model_copy(update={"history": list(history)}, deep=True)
        if self.settings.pii_masking:
            prepared.message = mask_pii(prepared.message)
            for message in [*prepared.history, *prepared.replies]:
                message.text = mask_pii(message.text)
        return prepared

    def system_prompt(self) -> str:
        kb = self.kb_store.current
        key = (kb.version, self.settings.upsell_in_reply)
        if key not in self._system_prompts:
            self._system_prompts.clear()  # храним только текущую версию БЗ
            self._system_prompts[key] = build_system_prompt(kb, upsell_in_reply=self.settings.upsell_in_reply)
        return self._system_prompts[key]

    async def suggest(
        self, request: SuggestRequest, mode: Mode = "full", provider: str | None = None
    ) -> SuggestResult:
        """provider — модель из реестра; None — модель по умолчанию, а если она не ответила, запасные."""
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
        used, llm, response, attempts, usage, notes = await self._generate_with_fallbacks(call, provider)
        suggestion, warnings = apply_guards(response.suggestion, kb, prepared, mode)
        warnings = notes + warnings

        meta = Meta(
            suggestion_id=uuid.uuid4().hex,
            mode=mode,
            llm_mode=llm.mode,
            provider=used,
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
            "provider": meta.provider,
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

    async def _generate_with_fallbacks(
        self, call: LLMCall, provider: str | None
    ) -> tuple[str, LLMClient, LLMResponse, int, Usage, list[GuardWarning]]:
        """Модель по цепочке реестра: если модель не ответила (недоступна, отказалась, сломала формат),
        спрашиваем следующую. Запасные, которые заведомо не ответят (нет ключа), пропускаем."""
        attempts = 0
        spent = Usage()
        failed: list[str] = []
        last_error: LLMError | None = None
        for current, llm in self.llms.chain(provider):
            if failed and llm.problem:
                continue
            try:
                response, tries, usage = await self._generate(llm, call)
            except LLMError as exc:
                attempts += 1
                spent = spent + exc.usage
                failed.append(current)
                last_error = exc
                logger.warning(
                    "llm_failed",
                    extra={
                        "fields": {"provider": current, "error_type": type(exc).__name__, "error": str(exc)}
                    },
                )
                continue
            notes: list[GuardWarning] = []
            if failed:
                names = ", ".join(self.llms.label(p) for p in failed)
                verb = "не ответила" if len(failed) == 1 else "не ответили"
                noun = "Модель" if len(failed) == 1 else "Модели"
                notes.append(
                    GuardWarning(
                        code="llm_fallback",
                        message=f"{noun} {names} {verb}, подсказку подготовила запасная модель "
                        f"{self.llms.label(current)}.",
                    )
                )
            return current, llm, response, attempts + tries, spent + usage, notes
        assert last_error is not None  # в цепочке всегда есть хотя бы одна модель
        raise last_error

    async def _generate(self, llm: LLMClient, call: LLMCall) -> tuple[LLMResponse, int, Usage]:
        """Вызов LLM с повторами: обрезанный ответ — один раз с удвоенным лимитом;
        невалидный или пустой ответ — ещё одна попытка."""
        attempts = 0
        usage = Usage()
        truncated_retry = bad_output_retry = False
        while True:
            attempts += 1
            try:
                response = await llm.generate(call)
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
