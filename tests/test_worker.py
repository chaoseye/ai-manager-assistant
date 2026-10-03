"""Очередь и обработчик: SQLite на диске, поддельный amoCRM, FakeLLM и управляемые часы."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from app.amocrm.client import AmoClient
from app.amocrm.fake import FakeAmoApi
from app.amocrm.webhooks import ChatMessageEvent, WebhookBatch
from app.config import BASE_DIR
from app.core.assistant import Assistant
from app.core.llm import LLMCall, LLMRefusedError, LLMResponse, LLMUnavailableError
from app.core.prompts import REPLIES_NOTE
from app.storage.db import Database, to_iso
from app.storage.repo import DialogRepo, JobRepo, StoredMessage, SuggestionRepo
from app.worker.processor import UNSEEN_REPLY_TEXT, Inbox, Worker, merge_by_time, split_dialog
from tests.conftest import FakeLLM, make_settings, make_suggestion

SEED = BASE_DIR / "examples" / "amocrm" / "mock_account.json"
T0 = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class HookedLLM(FakeLLM):
    """FakeLLM, который во время генерации выполняет действие — например, «присылает» новое сообщение."""

    def __init__(self, *outcomes, hook: Callable[[], Awaitable[None]] | None = None):
        super().__init__(*outcomes)
        self.hook = hook

    async def generate(self, call: LLMCall) -> LLMResponse:
        if self.hook is not None:
            hook, self.hook = self.hook, None
            await hook()
        return await super().generate(call)


def incoming(
    msg_id, text, *, at=T0, chat="chat-1", lead=1234, element_type=2, contact=3001234, attachment=None
):
    return ChatMessageEvent(
        id=msg_id,
        direction="in",
        chat_id=chat,
        talk_id="17",
        contact_id=contact,
        element_id=lead,
        element_type=element_type,
        author_type="contact",
        author_name="Анна из Telegram",
        text=text,
        attachment_type=attachment,
        origin="telegram",
        created_at=at,
    )


def outgoing(msg_id, text, *, at=T0, chat="chat-1", bot=False):
    return ChatMessageEvent(
        id=msg_id,
        direction="out",
        chat_id=chat,
        talk_id="17",
        contact_id=3001234,
        author_type="bot" if bot else "user",
        author_name="Salesbot" if bot else "Ольга",
        author_user_id=None if bot else 555,
        text=text,
        origin="telegram",
        created_at=at,
    )


@pytest.fixture
async def env(tmp_path, kb_store):
    settings = make_settings(tmp_path, amocrm_mode="mock", webhook_secret="s", job_retry_seconds=60)
    db = Database(settings.db_path)
    await db.connect()
    fake = FakeAmoApi.from_file(SEED, token="t")
    clock = Clock()
    dialogs, jobs, suggestions = DialogRepo(db), JobRepo(db), SuggestionRepo(db)
    ns = SimpleNamespace(settings=settings, db=db, fake=fake, clock=clock, jobs=jobs, suggestions=suggestions)
    ns.handler = fake.handle  # тест может подменить ответы amoCRM
    transport = httpx.MockTransport(lambda request: ns.handler(request))
    amo = AmoClient("https://klimat-demo.amocrm.ru", "t", rps=1000, max_retries=0, transport=transport)
    ns.amo = amo

    def make_worker(llm, **overrides):
        worker_settings = settings.model_copy(update=overrides) if overrides else settings
        ns.llm = llm
        ns.inbox = Inbox(db, dialogs, jobs, worker_settings, clock)
        ns.worker = Worker(
            settings=worker_settings,
            assistant=Assistant(kb_store, llm, worker_settings),
            amo=amo,
            db=db,
            dialogs=dialogs,
            jobs=jobs,
            suggestions=suggestions,
            clock=clock,
        )
        return ns.worker

    ns.make_worker = make_worker
    ns.ingest = lambda *events: ns.inbox.ingest(WebhookBatch(messages=list(events)))
    make_worker(FakeLLM(make_suggestion()))
    yield ns
    await amo.aclose()
    await db.close()


async def job_statuses(env) -> dict[str, int]:
    return await env.jobs.counts()


# ---------- Разбор диалога ----------


def _msg(amo_id, direction, author="contact", text="x"):
    return StoredMessage(amo_id, direction, author, None, text, None, T0)


def test_split_dialog():
    messages = [
        _msg("1", "in"),
        _msg("2", "out", "user"),
        _msg("3", "in"),
        _msg("4", "in"),
        _msg("5", "out", "bot"),
    ]
    split = split_dialog(messages)
    assert [m.amo_id for m in split.history] == ["1", "2"]
    assert [m.amo_id for m in split.run] == ["3", "4"]
    assert [m.amo_id for m in split.after] == ["5"]
    assert split.trigger_id == "4" and not split.manager_replied
    assert split_dialog([_msg("1", "out", "user")]) is None
    assert split_dialog([_msg("1", "in"), _msg("2", "out", "user")]).manager_replied


def test_merge_by_time_puts_unseen_reply_first_within_a_second():
    stored = [_msg("1", "in"), _msg("2", "in")]
    same_second = StoredMessage("r", "out", "user", None, "ответ", None, T0)
    assert [m.amo_id for m in merge_by_time(stored, [same_second])] == ["r", "1", "2"]
    later = StoredMessage("r2", "out", "user", None, "ответ", None, T0 + timedelta(seconds=1))
    assert [m.amo_id for m in merge_by_time(stored, [later])] == ["1", "2", "r2"]


# ---------- Основной путь ----------


async def test_debounce_merges_messages_and_posts_note(env):
    result = await env.ingest(incoming("m1", "Здравствуйте!"))
    assert result == {"accepted": 1, "duplicates": 0, "scheduled": 1, "late": 0, "skipped": 0}
    env.clock.advance(3)
    assert await env.worker.tick() == 0  # пауза ещё не прошла
    await env.ingest(incoming("m2", "Сколько стоит монтаж?", at=T0 + timedelta(seconds=3)))
    env.clock.advance(4)  # 7 с от первого, 4 с от второго — рано
    assert await env.worker.tick() == 0
    env.clock.advance(2)
    assert await env.worker.tick() == 1

    call = env.llm.calls[0]
    assert call.mode == "full"
    assert "<new_message>\nЗдравствуйте!\nСколько стоит монтаж?\n</new_message>" in call.user
    assert "Имя клиента: Анна" in call.user  # из карточки контакта, а не из мессенджера
    assert "этап: Первичный контакт" in call.user

    [note] = env.fake.list_notes("leads", 1234)
    assert note["params"]["service"] == "AI-помощник"
    assert note["params"]["text"].startswith("Черновик ответа клиенту:\nСтандартный монтаж стоит 9 900 ₽")
    assert "Допродажа (только для менеджера)" in note["params"]["text"]
    assert await job_statuses(env) == {"done": 1}

    cursor = await env.db.conn.execute("SELECT note_id, trigger_message_id, dialog_id FROM suggestions")
    row = await cursor.fetchone()
    assert row["note_id"] == note["id"] and row["trigger_message_id"] == "m2" and row["dialog_id"]


async def test_duplicate_webhook_is_ignored(env):
    await env.ingest(incoming("m1", "Привет"))
    result = await env.ingest(incoming("m1", "Привет"))
    assert result["duplicates"] == 1 and result["scheduled"] == 0
    cursor = await env.db.conn.execute("SELECT COUNT(*) FROM messages")
    assert (await cursor.fetchone())[0] == 1


async def test_context_from_lead_card(env):
    await env.ingest(incoming("m1", "Когда приедет мастер?", lead=1236, contact=3001236))
    env.clock.advance(6)
    await env.worker.tick()
    user = env.llm.calls[0].user
    assert "Имя клиента: Ирина" in user
    assert "этап: Монтаж назначен" in user
    assert "Товары в сделке: Сплит-система Basic 09, Стандартный монтаж" in user
    assert "Бюджет: 42800" in user
    assert env.fake.list_notes("leads", 1236)


async def test_chat_bound_to_contact_uses_open_lead(env):
    await env.ingest(incoming("m1", "Что по заказу?", lead=3001300, element_type=1, contact=3001300))
    env.clock.advance(6)
    await env.worker.tick()
    assert env.fake.list_notes("leads", 1301)
    assert not env.fake.list_notes("leads", 1300)


async def test_no_lead_and_no_contact(env):
    await env.ingest(incoming("m1", "Привет", lead=777, contact=None))
    env.clock.advance(6)
    await env.worker.tick()
    assert env.fake.notes == []
    cursor = await env.db.conn.execute("SELECT status, error FROM jobs")
    row = await cursor.fetchone()
    assert row["status"] == "done" and "некуда" in row["error"]


# ---------- Реплики менеджера ----------


async def test_manager_replied_before_processing_gives_upsell_only(env):
    env.make_worker(FakeLLM(make_suggestion(client_reply="", kb_refs=[])))
    await env.ingest(incoming("m1", "Сколько стоит монтаж?"))
    await env.ingest(outgoing("o1", "Монтаж — 9 900 ₽", at=T0 + timedelta(seconds=2)))
    env.clock.advance(6)
    await env.worker.tick()
    call = env.llm.calls[0]
    assert call.mode == "upsell_only"
    assert "<replies>" in call.user and "Менеджер (Ольга): Монтаж — 9 900 ₽" in call.user
    [note] = env.fake.notes
    assert note["params"]["text"].startswith("Менеджер уже ответил клиенту — черновик не нужен.")


async def test_manager_replied_skip_mode(env):
    worker = env.make_worker(FakeLLM(make_suggestion()), on_manager_replied="skip")
    await env.ingest(
        incoming("m1", "Сколько стоит монтаж?"), outgoing("o1", "9 900 ₽", at=T0 + timedelta(seconds=1))
    )
    env.clock.advance(6)
    await worker.tick()
    assert env.llm.calls == [] and env.fake.notes == []
    assert await job_statuses(env) == {"skipped": 1}


async def test_bot_reply_keeps_full_mode(env):
    await env.ingest(
        incoming("m1", "Сколько стоит монтаж?"),
        outgoing("o1", "Спасибо! Менеджер скоро ответит.", at=T0 + timedelta(seconds=1), bot=True),
    )
    env.clock.advance(6)
    await env.worker.tick()
    call = env.llm.calls[0]
    assert call.mode == "full"
    assert "Бот (Salesbot): Спасибо! Менеджер скоро ответит." in call.user


async def test_client_writes_during_generation_marks_stale(env):
    async def new_message() -> None:
        await env.ingest(incoming("m2", "И ещё вопрос", at=T0 + timedelta(seconds=7)))

    env.make_worker(HookedLLM(make_suggestion(), hook=new_message))
    await env.ingest(incoming("m1", "Сколько стоит монтаж?"))
    env.clock.advance(6)
    await env.worker.tick()
    assert env.fake.notes == []
    assert await job_statuses(env) == {"stale": 1, "pending": 1}

    env.clock.advance(6)
    await env.worker.tick()
    assert len(env.fake.notes) == 1
    assert "Сколько стоит монтаж?\nИ ещё вопрос" in env.llm.calls[-1].user


async def test_manager_replies_during_generation_drops_draft(env):
    async def manager_reply() -> None:
        await env.ingest(outgoing("o1", "Уже отвечаю", at=T0 + timedelta(seconds=7)))

    env.make_worker(HookedLLM(make_suggestion(), hook=manager_reply))
    await env.ingest(incoming("m1", "Сколько стоит монтаж?"))
    env.clock.advance(6)
    await env.worker.tick()
    [note] = env.fake.notes
    assert note["params"]["text"].startswith("Менеджер уже ответил клиенту")
    assert "Допродажа" in note["params"]["text"]


async def test_attachment_without_text(env):
    await env.ingest(incoming("m1", "", attachment="voice"))
    env.clock.advance(6)
    await env.worker.tick()
    assert env.llm.calls == []
    [note] = env.fake.notes
    assert "вложение без текста (voice)" in note["params"]["text"]


async def test_attachment_with_text_goes_to_model(env):
    await env.ingest(incoming("m1", "Вот фото окна", attachment="picture"))
    env.clock.advance(6)
    await env.worker.tick()
    assert "Вот фото окна [вложение: picture]" in env.llm.calls[0].user


async def test_invisible_text_is_skipped_without_model(env):
    # Символы нулевой ширины — не текст: запрос к модели не прошёл бы проверку, а примечание не нужно.
    await env.ingest(incoming("m1", "​⁠﻿"))
    env.clock.advance(6)
    await env.worker.tick()
    assert env.llm.calls == [] and env.fake.notes == []
    assert await job_statuses(env) == {"skipped": 1}


async def test_invisible_text_with_attachment_is_an_attachment(env):
    await env.ingest(incoming("m1", "​", attachment="picture"))
    env.clock.advance(6)
    await env.worker.tick()
    assert env.llm.calls == []
    [note] = env.fake.notes
    assert "вложение без текста (picture)" in note["params"]["text"]


# ---------- Ошибки и повторы ----------


async def test_llm_unavailable_is_retried_then_failure_note(env):
    env.make_worker(FakeLLM(LLMUnavailableError("нет сети")))
    await env.ingest(incoming("m1", "Сколько стоит монтаж?"))
    env.clock.advance(6)
    await env.worker.tick()
    assert await job_statuses(env) == {"pending": 1}
    env.clock.advance(59)
    assert await env.worker.tick() == 0  # ждём job_retry_seconds
    env.clock.advance(1)
    await env.worker.tick()
    env.clock.advance(60)
    await env.worker.tick()
    assert await job_statuses(env) == {"failed": 1}
    [note] = env.fake.notes
    assert note["params"]["text"] == (
        "Подсказка не сформирована: AI-модель сейчас недоступна. Ответьте клиенту самостоятельно."
    )


async def test_refusal_fails_without_retry(env):
    env.make_worker(FakeLLM(LLMRefusedError("отказ")))
    await env.ingest(incoming("m1", "…"))
    env.clock.advance(6)
    await env.worker.tick()
    assert await job_statuses(env) == {"failed": 1}
    assert "модель отказалась отвечать" in env.fake.notes[0]["params"]["text"]


async def test_note_retry_reuses_generated_suggestion(env):
    failures = {"left": 1}

    def flaky(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and failures["left"]:
            failures["left"] -= 1
            return httpx.Response(503)
        return env.fake.handle(request)

    env.handler = flaky
    await env.ingest(incoming("m1", "Сколько стоит монтаж?"))
    env.clock.advance(6)
    await env.worker.tick()
    assert await job_statuses(env) == {"pending": 1}
    env.clock.advance(60)
    await env.worker.tick()
    assert len(env.llm.calls) == 1  # вторая попытка только публикует
    assert len(env.fake.notes) == 1
    assert await job_statuses(env) == {"done": 1}


async def test_new_message_supersedes_retry(env):
    env.make_worker(FakeLLM(LLMUnavailableError("нет сети")))
    await env.ingest(incoming("m1", "Первое"))
    env.clock.advance(6)
    [job] = await env.jobs.claim_due(env.clock(), 5)
    await env.ingest(incoming("m2", "Второе", at=env.clock()))  # пока задача в работе, пришло новое
    await env.worker.process(job)
    assert await job_statuses(env) == {"stale": 1, "pending": 1}


async def test_recover_running_after_restart(env):
    await env.ingest(incoming("m1", "Привет"))
    env.clock.advance(6)
    await env.jobs.claim_due(env.clock(), 5)
    assert await job_statuses(env) == {"running": 1}
    assert await env.jobs.recover_running(env.clock()) == 1
    assert await job_statuses(env) == {"pending": 1}


async def test_retention_cleanup(env):
    await env.ingest(incoming("m1", "Привет"))
    env.clock.advance(6)
    await env.worker.tick()
    # Время подсказки ассистент берёт из настоящих часов, остальное — из часов теста. Переводим часы
    # теста дальше обоих: с T0 + 31 день тест начинал падать, как только настоящая дата его переходила.
    later = max(env.clock.now, datetime.now(UTC)) + timedelta(days=env.settings.retention_days + 1)
    env.clock.advance((later - env.clock.now).total_seconds())
    await env.worker._maybe_cleanup()
    for table in ("messages", "suggestions", "jobs", "dialogs"):
        cursor = await env.db.conn.execute(f"SELECT COUNT(*) FROM {table}")
        assert (await cursor.fetchone())[0] == 0, table


async def test_background_loop_processes_and_stops(env):
    worker = env.make_worker(FakeLLM(make_suggestion()), debounce_seconds=0, worker_poll_seconds=0.01)
    await worker.start()
    try:
        await env.ingest(incoming("m1", "Сколько стоит монтаж?"))
        for _ in range(200):
            if env.fake.notes:
                break
            await asyncio.sleep(0.01)
        assert len(env.fake.notes) == 1
        assert worker.running
    finally:
        await worker.stop()
    assert not worker.running


async def test_messages_are_ordered_by_amo_time(env):
    late_first = incoming("m2", "второе", at=T0 + timedelta(seconds=10))
    early = incoming("m1", "первое", at=T0)
    await env.ingest(late_first)
    await env.ingest(early)  # пришло позже, но написано раньше
    env.clock.advance(6)
    await env.worker.tick()
    assert "<new_message>\nпервое\nвторое\n</new_message>" in env.llm.calls[0].user
    assert to_iso(T0).endswith("+00:00")


# ---------- Опоздавшие вебхуки ----------
# На живом аккаунте 03.10.2026 часть вебхуков пришла на минуты позже: «Здравствуйте» — через 104 с,
# все три ответа менеджера — через 136–300 с. Сервис отвечал на каждый за десятки миллисекунд.


def ts(moment: datetime) -> int:
    return int(moment.timestamp())


async def test_late_older_message_after_processing_is_only_stored(env):
    await env.ingest(incoming("m2", "Сколько стоит монтаж?", at=T0 + timedelta(seconds=4)))
    env.clock.advance(6)
    await env.worker.tick()
    result = await env.ingest(incoming("m1", "Здравствуйте", at=T0))  # написано раньше, дошло позже
    assert result["late"] == 1 and result["scheduled"] == 0
    env.clock.advance(6)
    assert await env.worker.tick() == 0
    assert len(env.fake.notes) == 1 and len(env.llm.calls) == 1
    assert await job_statuses(env) == {"done": 1}
    cursor = await env.db.conn.execute("SELECT COUNT(*) FROM messages")
    assert (await cursor.fetchone())[0] == 2  # в историю встало


async def test_late_message_does_not_move_pending_job(env):
    await env.ingest(incoming("m2", "второе", at=T0 + timedelta(seconds=10)))
    env.clock.advance(4)
    await env.ingest(incoming("m1", "первое", at=T0))
    cursor = await env.db.conn.execute("SELECT trigger_message_id, run_at FROM jobs")
    row = await cursor.fetchone()
    assert row["trigger_message_id"] == "m2" and row["run_at"] == to_iso(T0 + timedelta(seconds=6))
    env.clock.advance(2)  # пауза считается от второго сообщения, опоздавшее её не сдвигает
    assert await env.worker.tick() == 1
    assert "<new_message>\nпервое\nвторое\n</new_message>" in env.llm.calls[0].user


async def test_messages_in_the_same_second_are_not_late(env):
    first = await env.ingest(incoming("m1", "Здравствуйте"))
    second = await env.ingest(incoming("m2", "Сколько стоит монтаж?"))  # то же время amoCRM
    assert first["scheduled"] == second["scheduled"] == 1 and second["late"] == 0


async def test_late_message_during_generation_keeps_suggestion(env):
    async def late_message() -> None:
        await env.ingest(incoming("m0", "Здравствуйте", at=T0 - timedelta(seconds=5)))

    env.make_worker(HookedLLM(make_suggestion(), hook=late_message))
    await env.ingest(incoming("m1", "Сколько стоит монтаж?"))
    env.clock.advance(6)
    await env.worker.tick()
    assert await job_statuses(env) == {"done": 1}  # не stale и без новой задачи
    [note] = env.fake.notes
    assert note["params"]["text"].startswith("Черновик ответа клиенту:")


async def test_manager_reply_seen_only_in_events_gives_upsell_only(env):
    env.make_worker(FakeLLM(make_suggestion(client_reply="", kb_refs=[])))
    await env.ingest(incoming("m1", "Сколько стоит монтаж?"))
    # Менеджер ответил через 3 с, вебхук об этом ещё в пути, а журнал событий уже знает.
    env.fake.add_chat_event("o-late", talk_id=17, created_at=ts(T0) + 3, created_by=555)
    env.clock.advance(6)
    await env.worker.tick()
    call = env.llm.calls[0]
    assert call.mode == "upsell_only"
    reply = f"[2026-09-29 12:00] Менеджер: {UNSEEN_REPLY_TEXT}"
    assert f"<replies>\n{REPLIES_NOTE}\n{reply}\n</replies>" in call.user
    [note] = env.fake.notes
    assert note["params"]["text"].startswith("Менеджер уже ответил клиенту — черновик не нужен.")


async def test_reply_in_events_splits_the_run(env):
    # Как на живом аккаунте: клиент спросил, менеджер ответил (вебхук опаздывает), клиент написал ещё.
    await env.ingest(
        incoming("m1", "Сколько стоит монтаж?"),
        incoming("m2", "А доставка сколько?", at=T0 + timedelta(seconds=40)),
    )
    env.fake.add_chat_event("o-late", talk_id=17, created_at=ts(T0) + 20, created_by=555)
    env.clock.advance(6)
    await env.worker.tick()
    call = env.llm.calls[0]
    assert call.mode == "full"
    assert "<new_message>\nА доставка сколько?\n</new_message>" in call.user  # отвеченное — уже история
    assert f"Клиент: Сколько стоит монтаж?\n[2026-09-29 12:00] Менеджер: {UNSEEN_REPLY_TEXT}" in call.user


async def test_events_older_than_window_are_not_queried(env):
    # Клиент пишет три часа без ответа; ответ старше двух часов вебхук уже доставил бы сам.
    await env.ingest(
        incoming("m1", "Сколько стоит монтаж?", at=T0 - timedelta(hours=3)),
        incoming("m2", "Ау?", at=T0),
    )
    old = ts(T0 - timedelta(hours=2, minutes=30))
    env.fake.add_chat_event("o-old", talk_id=17, created_at=old, created_by=555)
    env.clock.advance(6)
    await env.worker.tick()
    assert "<new_message>\nСколько стоит монтаж?\nАу?\n</new_message>" in env.llm.calls[0].user


async def test_reply_in_events_before_voice_gives_attachment_note(env):
    await env.ingest(
        incoming("m1", "Сколько стоит монтаж?"),
        incoming("m2", "", at=T0 + timedelta(seconds=40), attachment="voice"),
    )
    env.fake.add_chat_event("o-late", talk_id=17, created_at=ts(T0) + 20, created_by=555)
    env.clock.advance(6)
    await env.worker.tick()
    assert env.llm.calls == []
    [note] = env.fake.notes
    assert "вложение без текста (voice)" in note["params"]["text"]


async def test_manager_reply_in_events_with_skip_mode(env):
    worker = env.make_worker(FakeLLM(make_suggestion()), on_manager_replied="skip")
    await env.ingest(incoming("m1", "Сколько стоит монтаж?"))
    env.fake.add_chat_event("o-late", talk_id=17, created_at=ts(T0) + 3, created_by=555)
    env.clock.advance(6)
    await worker.tick()
    assert env.llm.calls == [] and env.fake.notes == []
    cursor = await env.db.conn.execute("SELECT status, error FROM jobs")
    row = await cursor.fetchone()
    assert row["status"] == "skipped" and "журнале событий" in row["error"]


@pytest.mark.parametrize(
    ("talk_id", "created_by", "delay"),
    [
        (17, 0, 3),  # бот или интеграция: created_by = 0
        (99, 555, 3),  # другая беседа
        (17, 555, 0),  # в ту же секунду, что и вопрос, — не ответ на него
        (17, 555, -30),  # раньше вопроса
    ],
)
async def test_events_that_are_not_a_reply_keep_full_mode(env, talk_id, created_by, delay):
    await env.ingest(incoming("m1", "Сколько стоит монтаж?"))
    env.fake.add_chat_event("o-x", talk_id=talk_id, created_at=ts(T0) + delay, created_by=created_by)
    env.clock.advance(6)
    await env.worker.tick()
    assert env.llm.calls[0].mode == "full"
    assert env.fake.notes[0]["params"]["text"].startswith("Черновик ответа клиенту:")


async def test_delivered_message_is_not_recounted_from_events(env):
    # Вебхук уже пришёл и назвал автора ботом (нет user_id) — верим ему, а не повторной записи в журнале.
    await env.ingest(
        incoming("m1", "Сколько стоит монтаж?"),
        outgoing("o1", "Спасибо! Менеджер скоро ответит.", at=T0 + timedelta(seconds=1), bot=True),
    )
    env.fake.add_chat_event("o1", talk_id=17, created_at=ts(T0) + 1, created_by=555)
    env.clock.advance(6)
    await env.worker.tick()
    assert env.llm.calls[0].mode == "full"


@pytest.mark.parametrize(("lead", "mode"), [(1234, "upsell_only"), (1236, "full")])
async def test_without_talk_id_reply_is_matched_by_lead(env, lead, mode):
    await env.ingest(incoming("m1", "Сколько стоит монтаж?").model_copy(update={"talk_id": None}))
    env.fake.add_chat_event("o-late", talk_id=None, created_at=ts(T0) + 3, created_by=555, entity_id=lead)
    env.clock.advance(6)
    await env.worker.tick()
    assert env.llm.calls[0].mode == mode


async def test_reply_during_generation_seen_in_events_drops_draft(env):
    async def manager_reply() -> None:
        env.fake.add_chat_event("o-late", talk_id=17, created_at=ts(T0) + 7, created_by=555)

    env.make_worker(HookedLLM(make_suggestion(), hook=manager_reply))
    await env.ingest(incoming("m1", "Сколько стоит монтаж?"))
    env.clock.advance(6)
    await env.worker.tick()
    assert env.llm.calls[0].mode == "full"
    [note] = env.fake.notes
    assert note["params"]["text"].startswith("Менеджер уже ответил клиенту")
    assert env.fake.requests.count(("GET", "/api/v4/events")) == 2  # до генерации и перед публикацией


async def test_events_failure_falls_back_to_webhooks(env, caplog):
    def broken_events(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v4/events":
            return httpx.Response(500)
        return env.fake.handle(request)

    env.handler = broken_events
    await env.ingest(incoming("m1", "Сколько стоит монтаж?"))
    env.clock.advance(6)
    await env.worker.tick()
    assert env.llm.calls[0].mode == "full" and len(env.fake.notes) == 1
    assert await job_statuses(env) == {"done": 1}
    assert any(r.getMessage() == "amocrm_events_unavailable" for r in caplog.records)


async def test_live_session_2026_10_03_replay(env):
    """Сессия на живом аккаунте 03.10.2026 по секундам. Секунда 0 — 12:17:00. Каждое сообщение описано так:
    время в amoCRM, когда пришёл вебхук, направление, текст. Ответы менеджера видны в журнале сразу,
    а их вебхуки опаздывали на 136–300 с. Тогда вышло 6 примечаний, 5 из них — черновики на один и тот же
    блок сообщений. Теперь каждое примечание отвечает на то, что клиент написал после ответа менеджера."""
    worker = env.make_worker(FakeLLM(make_suggestion()), debounce_seconds=20)
    timeline = [
        (0, 1, "bot", "Оцените качество обслуживания от 1 до 10"),
        (392, 496, "in", "Здравствуйте"),  # вебхук опоздал на 104 с
        (396, 398, "in", "Нужен кондиционер в спальню 20 м²"),
        (400, 401, "in", "Сколько будет с установкой?"),
        (422, 557, "user", "Сейчас посчитаю"),
        (466, 469, "voice", ""),
        (485, 785, "user", "Вы прислали пустое сообщение"),  # 300 с — повтор amoCRM
        (515, 518, "picture", "Такой подойдет?"),
        (679, 878, "user", "Отправьте голосовое еще раз"),
        (704, 708, "voice", ""),
        (1059, 1085, "voice", ""),
    ]
    actions = []
    for i, (written, delivered, kind, text) in enumerate(timeline):
        at, msg_id = T0 + timedelta(seconds=written), f"msg-{i}"
        if kind in ("user", "bot"):
            event = outgoing(msg_id, text, at=at, bot=kind == "bot")
            actions.append((written, lambda m=msg_id, w=written, k=kind: env.fake.add_chat_event(
                m, talk_id=17, created_at=ts(T0) + w, created_by=0 if k == "bot" else 555
            )))  # fmt: skip
        else:
            attachment = kind if kind in ("voice", "picture") else None
            event = incoming(msg_id, text, at=at, attachment=attachment)
        actions.append((delivered, lambda e=event: env.ingest(e)))
    actions.sort(key=lambda item: item[0])
    for second in range(0, 1130):
        env.clock.now = T0 + timedelta(seconds=second)
        for _, action in [a for a in actions if a[0] == second]:
            result = action()
            if asyncio.iscoroutine(result):
                await result
        await worker.tick()

    kinds = [
        "черновик" if text.startswith("Черновик ответа клиенту:") else
        "голосовое" if "вложение без текста (voice)" in text else text
        for text in (note["params"]["text"] for note in env.fake.notes)
    ]  # fmt: skip
    assert kinds == [
        "черновик",  # 12:24:01, менеджер ещё не ответил
        "голосовое",  # голосовое после ответа «Сейчас посчитаю», о котором вебхук ещё не пришёл
        "черновик",  # только фото с подписью: всё до него уже отвечено
        "голосовое",
        "голосовое",
    ]
    assert [c.mode for c in env.llm.calls] == ["full", "full"]
    assert "<new_message>\nТакой подойдет? [вложение: picture]\n</new_message>" in env.llm.calls[1].user
    assert await job_statuses(env) == {"done": 5}  # «Здравствуйте» задачу не поставило


async def test_attachment_only_does_not_query_events(env):
    # Примечание о вложении пишется и после ответа менеджера, журнал тут ничего не меняет.
    await env.ingest(incoming("m1", "", attachment="voice"))
    env.clock.advance(6)
    await env.worker.tick()
    assert ("GET", "/api/v4/events") not in env.fake.requests
    assert "вложение без текста (voice)" in env.fake.notes[0]["params"]["text"]


async def test_concurrent_returning_and_commit_do_not_collide(tmp_path):
    # Вебхук (upsert диалога с RETURNING) и обработчик (claim_due с RETURNING, commit) работают
    # на одном соединении. Пока курсор RETURNING не дочитан, чужой commit падал с
    # «cannot commit transaction - SQL statements in progress» — изредка ронял и тест имитатора.
    db = Database(tmp_path / "race.db")
    await db.connect()
    dialogs, jobs = DialogRepo(db), JobRepo(db)
    try:

        async def webhook_side():
            for i in range(150):
                dialog_id = await dialogs.upsert(incoming(f"m{i}", "Привет", chat=f"chat-{i % 7}"), T0)
                await jobs.schedule(dialog_id, f"m{i}", T0, T0)
                await db.commit()

        async def worker_side():
            for _ in range(150):
                for job in await jobs.claim_due(T0 + timedelta(seconds=1), limit=5):
                    await jobs.finish(job.id, "done", T0)
                await jobs.counts()

        await asyncio.gather(webhook_side(), worker_side())
    finally:
        await db.close()
