"""Приём сообщений из amoCRM и очередь генерации подсказок.

Inbox вызывается из вебхука и делает только быстрые операции с БД: amoCRM ждёт ответ не больше 2 секунд.
Worker в фоне забирает созревшие задачи: собирает контекст, вызывает ядро и пишет примечание в сделку.
"""

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.amocrm.client import AmoClient, AmoError, NoteEntity
from app.amocrm.context import resolve_context
from app.amocrm.notes import format_attachment_note, format_failure_note, format_suggestion_note
from app.amocrm.webhooks import ELEMENT_LEAD, WebhookBatch
from app.config import Settings
from app.core.assistant import Assistant
from app.core.llm import LLMError, LLMRefusedError, LLMUnavailableError
from app.core.schemas import DialogMessage, LeadContext, Mode, Role, SuggestRequest, SuggestResult
from app.storage.db import Database, utcnow
from app.storage.repo import (
    JOB_DONE,
    JOB_FAILED,
    JOB_PENDING,
    JOB_SKIPPED,
    JOB_STALE,
    Dialog,
    DialogRepo,
    Job,
    JobRepo,
    StoredMessage,
    SuggestionRepo,
    cleanup,
)

logger = logging.getLogger(__name__)

Clock = Callable[[], datetime]

MAX_TEXT = 10_000
MAX_HISTORY = 500
MAX_REPLIES = 50
CLEANUP_INTERVAL = timedelta(hours=24)


# ---------- Разбор диалога ----------


@dataclass(frozen=True)
class DialogSplit:
    history: list[StoredMessage]
    run: list[StoredMessage]  # подряд идущие последние сообщения клиента — новое обращение
    after: list[StoredMessage]  # что отправили после него: менеджер, бот

    @property
    def trigger_id(self) -> str:
        return self.run[-1].amo_id

    @property
    def manager_replied(self) -> bool:
        return any(m.author_type == "user" for m in self.after)

    @property
    def has_text(self) -> bool:
        return any(m.text.strip() for m in self.run)


def split_dialog(messages: list[StoredMessage]) -> DialogSplit | None:
    last_in = max((i for i, m in enumerate(messages) if m.direction == "in"), default=None)
    if last_in is None:
        return None
    start = last_in
    while start > 0 and messages[start - 1].direction == "in":
        start -= 1
    return DialogSplit(messages[:start], messages[start : last_in + 1], messages[last_in + 1 :])


def _clip(text: str, limit: int = MAX_TEXT) -> str:
    return text if len(text) <= limit else text[: limit - 20] + " […обрезано]"


def _message_text(message: StoredMessage) -> str:
    text = message.text.strip()
    if message.attachment_type:
        text = f"{text} [вложение: {message.attachment_type}]".strip()
    return text


def to_dialog_message(message: StoredMessage) -> DialogMessage:
    role: Role = "client"
    if message.direction == "out":
        role = "manager" if message.author_type == "user" else "bot"
    return DialogMessage(
        role=role,
        text=_clip(_message_text(message)),
        ts=message.created_at,
        author_name=message.author_name[:200] if message.author_name else None,
    )


def build_request(split: DialogSplit, lead: LeadContext | None, channel: str | None) -> SuggestRequest:
    message = "\n".join(text for text in (_message_text(m) for m in split.run) if text)
    return SuggestRequest(
        message=_clip(message),
        history=[to_dialog_message(m) for m in split.history[-MAX_HISTORY:]],
        replies=[to_dialog_message(m) for m in split.after[-MAX_REPLIES:]],
        lead=lead,
        channel=channel[:50] if channel else None,
    )


# ---------- Приём ----------


class Inbox:
    def __init__(
        self, db: Database, dialogs: DialogRepo, jobs: JobRepo, settings: Settings, clock: Clock = utcnow
    ):
        self._db = db
        self._dialogs = dialogs
        self._jobs = jobs
        self._settings = settings
        self._clock = clock

    async def ingest(self, batch: WebhookBatch) -> dict[str, int]:
        """Пишет сообщения в историю. Каждое новое сообщение клиента ставит или сдвигает задачу диалога."""
        now = self._clock()
        accepted = duplicates = scheduled = 0
        for event in batch.messages:
            dialog_id = await self._dialogs.upsert(event, now)
            if not await self._dialogs.add_message(dialog_id, event, now):
                duplicates += 1
                continue
            accepted += 1
            if event.direction == "in":
                run_at = now + timedelta(seconds=self._settings.debounce_seconds)
                await self._jobs.schedule(dialog_id, event.id, run_at, now)
                scheduled += 1
        await self._db.commit()
        result = {
            "accepted": accepted,
            "duplicates": duplicates,
            "scheduled": scheduled,
            "skipped": len(batch.skipped),
        }
        logger.info("webhook_ingested", extra={"fields": result})
        return result


# ---------- Очередь ----------


@dataclass
class _JobState:
    target: tuple[NoteEntity, int] | None = None
    fallback_target: tuple[NoteEntity, int] | None = None
    details: dict[str, object] = field(default_factory=dict)


def _friendly_reason(exc: Exception) -> str:
    if isinstance(exc, LLMRefusedError):
        return "модель отказалась отвечать на это сообщение"
    if isinstance(exc, LLMUnavailableError):
        return "AI-модель сейчас недоступна"
    if isinstance(exc, LLMError):
        return "модель вернула некорректный ответ"
    if isinstance(exc, AmoError):
        return "не удалось получить данные из amoCRM"
    return "внутренняя ошибка сервиса"


class Worker:
    def __init__(
        self,
        *,
        settings: Settings,
        assistant: Assistant,
        amo: AmoClient,
        db: Database,
        dialogs: DialogRepo,
        jobs: JobRepo,
        suggestions: SuggestionRepo,
        clock: Clock = utcnow,
    ):
        self.settings = settings
        self.assistant = assistant
        self.amo = amo
        self.db = db
        self.dialogs = dialogs
        self.jobs = jobs
        self.suggestions = suggestions
        self.clock = clock
        self._tasks: set[asyncio.Task[str]] = set()
        self._stop: asyncio.Event | None = None
        self._loop_task: asyncio.Task[None] | None = None
        self._last_cleanup: datetime | None = None

    @property
    def running(self) -> bool:
        return self._loop_task is not None and not self._loop_task.done()

    # ---------- Жизненный цикл ----------

    async def start(self) -> None:
        recovered = await self.jobs.recover_running(self.clock())
        if recovered:
            logger.warning("jobs_recovered", extra={"fields": {"count": recovered}})
        self._stop = asyncio.Event()
        self._loop_task = asyncio.create_task(self._run(), name="suggestion-worker")

    async def stop(self, timeout: float = 10.0) -> None:
        if self._loop_task is None or self._stop is None:
            return
        self._stop.set()
        await self._loop_task
        if self._tasks:
            _, pending = await asyncio.wait(self._tasks, timeout=timeout)
            for task in pending:
                task.cancel()  # останется running и будет подхвачена при следующем старте
            await asyncio.gather(*pending, return_exceptions=True)
        self._loop_task = None

    async def _run(self) -> None:
        assert self._stop is not None
        while not self._stop.is_set():
            try:
                await self._maybe_cleanup()
                free = self.settings.worker_concurrency - len(self._tasks)
                if free > 0:
                    for job in await self.jobs.claim_due(self.clock(), free):
                        task = asyncio.create_task(self.process(job), name=f"job-{job.id}")
                        self._tasks.add(task)
                        task.add_done_callback(self._tasks.discard)
            except Exception:
                logger.exception("worker_loop_error")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self.settings.worker_poll_seconds)

    async def tick(self) -> int:
        """Один проход без фонового цикла: забрать созревшие задачи и дождаться их. Для тестов и отладки."""
        jobs = await self.jobs.claim_due(self.clock(), self.settings.worker_concurrency)
        await asyncio.gather(*(self.process(job) for job in jobs))
        return len(jobs)

    async def _maybe_cleanup(self) -> None:
        now = self.clock()
        if self._last_cleanup and now - self._last_cleanup < CLEANUP_INTERVAL:
            return
        self._last_cleanup = now
        deleted = await cleanup(self.db, now - timedelta(days=self.settings.retention_days))
        if any(deleted.values()):
            logger.info("retention_cleanup", extra={"fields": deleted})

    # ---------- Обработка задачи ----------

    async def process(self, job: Job) -> str:
        """Обрабатывает задачу и возвращает её итоговый статус. Исключения не выпускает."""
        started = time.perf_counter()
        state = _JobState()
        try:
            status, detail = await self._process(job, state)
            await self.jobs.finish(job.id, status, self.clock(), error=detail or None)
        except LLMRefusedError as exc:
            status, detail = await self._fail(job, exc, state, permanent=True)
        except (LLMError, AmoError) as exc:
            status, detail = await self._fail(job, exc, state)
        except Exception as exc:
            logger.exception("job_unexpected_error", extra={"fields": {"job_id": job.id}})
            status, detail = await self._fail(job, exc, state)
        logger.info(
            "job_processed",
            extra={
                "fields": {
                    "job_id": job.id,
                    "dialog_id": job.dialog_id,
                    "status": status,
                    "detail": detail,
                    "attempt": job.attempts,
                    "duration_ms": round((time.perf_counter() - started) * 1000),
                    **state.details,
                }
            },
        )
        return status

    async def _process(self, job: Job, state: _JobState) -> tuple[str, str]:
        dialog = await self.dialogs.get(job.dialog_id)
        if dialog is None:
            return JOB_FAILED, "диалог не найден"
        state.fallback_target = self._dialog_target(dialog)
        split = split_dialog(await self.dialogs.messages(dialog.id))
        if split is None:
            return JOB_SKIPPED, "в диалоге нет сообщений клиента"

        mode: Mode = "full"
        if split.manager_replied:
            if self.settings.on_manager_replied == "skip":
                return JOB_SKIPPED, "менеджер уже ответил"
            mode = "upsell_only"
        state.details["mode"] = mode

        resolved = await resolve_context(
            self.amo,
            element_type=dialog.element_type,
            element_id=dialog.element_id,
            contact_id=dialog.contact_id,
            fallback_name=dialog.contact_name,
        )
        state.target = resolved.note_target
        state.details["note_target"] = (
            "/".join(map(str, resolved.note_target)) if resolved.note_target else None
        )

        if not split.has_text:
            if resolved.note_target is None:
                return JOB_DONE, "вложение без текста; сделка и контакт не найдены"
            note = format_attachment_note([m.attachment_type or "" for m in split.run])
            note_id = await self.amo.add_note(*resolved.note_target, note, self.settings.note_service_name)
            await self.dialogs.add_note(dialog.id, note_id, self.clock())
            return JOB_DONE, "вложение без текста: отправлено уведомление"

        result = await self._generate(job, split, dialog, resolved.lead, mode)
        state.details["suggestion_id"] = result.meta.suggestion_id

        # Пока шла генерация, диалог мог измениться.
        fresh = split_dialog(await self.dialogs.messages(dialog.id))
        if fresh is not None and fresh.trigger_id != split.trigger_id:
            return JOB_STALE, "пока шла генерация, клиент написал ещё — подсказка устарела"
        include_reply = mode == "full" and not (fresh is not None and fresh.manager_replied)

        if resolved.note_target is None:
            return JOB_DONE, "сделка и контакт не найдены — примечание некуда записать"
        text = format_suggestion_note(result, include_reply=include_reply)
        note_id = await self.amo.add_note(*resolved.note_target, text, self.settings.note_service_name)
        await self.suggestions.set_note_id(result.meta.suggestion_id, note_id)
        await self.dialogs.add_note(dialog.id, note_id, self.clock())
        state.details["note_id"] = note_id
        return JOB_DONE, ""

    async def _generate(
        self, job: Job, split: DialogSplit, dialog: Dialog, lead: LeadContext | None, mode: Mode
    ) -> SuggestResult:
        # Повтор после сбоя публикации: подсказка уже есть — не тратим токены второй раз.
        if job.suggestion_id:
            saved = await self.suggestions.get(job.suggestion_id)
            if (
                saved
                and saved["trigger_message_id"] == split.trigger_id
                and saved["mode"] == mode
                and saved["note_id"] is None
            ):
                return SuggestResult.model_validate(
                    {"suggestion": saved["suggestion"], "meta": saved["meta"]}
                )

        request = build_request(split, lead, dialog.origin)
        result, prepared = await self.assistant.suggest_with_request(request, mode)
        await self.suggestions.save(
            result, prepared, dialog_id=dialog.id, trigger_message_id=split.trigger_id
        )
        await self.jobs.set_suggestion(job.id, result.meta.suggestion_id, split.trigger_id)
        return result

    @staticmethod
    def _dialog_target(dialog: Dialog) -> tuple[NoteEntity, int] | None:
        if dialog.element_type == ELEMENT_LEAD and dialog.element_id:
            return ("leads", dialog.element_id)
        if dialog.contact_id:
            return ("contacts", dialog.contact_id)
        return None

    async def _fail(
        self, job: Job, exc: Exception, state: _JobState, *, permanent: bool = False
    ) -> tuple[str, str]:
        now = self.clock()
        detail = f"{type(exc).__name__}: {exc}"[:1000]
        if not permanent and job.attempts < self.settings.job_max_attempts:
            run_at = now + timedelta(seconds=self.settings.job_retry_seconds)
            requeued = await self.jobs.retry_later(job.id, run_at, detail, now)
            return (JOB_PENDING if requeued else JOB_STALE), detail
        await self.jobs.finish(job.id, JOB_FAILED, now, error=detail)
        target = state.target or state.fallback_target
        if target is not None:
            try:
                note = format_failure_note(_friendly_reason(exc))
                note_id = await self.amo.add_note(*target, note, self.settings.note_service_name)
                await self.dialogs.add_note(job.dialog_id, note_id, now)
            except AmoError as note_error:
                logger.warning(
                    "failure_note_not_sent", extra={"fields": {"job_id": job.id, "error": str(note_error)}}
                )
        return JOB_FAILED, detail
