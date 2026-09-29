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
from app.storage.db import Database, to_iso
from app.storage.repo import DialogRepo, JobRepo, StoredMessage, SuggestionRepo
from app.worker.processor import Inbox, Worker, split_dialog
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


# ---------- Основной путь ----------


async def test_debounce_merges_messages_and_posts_note(env):
    result = await env.ingest(incoming("m1", "Здравствуйте!"))
    assert result == {"accepted": 1, "duplicates": 0, "scheduled": 1, "skipped": 0}
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
    env.clock.advance(31 * 24 * 3600)
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
