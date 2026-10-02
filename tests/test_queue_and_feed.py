"""Очередь и лента страницы «amoCRM (mock)»: параллельные проходы, запоздавшие сообщения, несколько чатов
одной сделки, настройки очереди.

xfail(strict=True) — известный недочёт (см. шапку test_money_pii_props.py).
"""

import asyncio
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.llm import LLMRefusedError
from app.main import create_app
from tests.conftest import FakeLLM, make_settings, make_suggestion
from tests.test_worker import T0, env, incoming, outgoing  # noqa: F401  (env — фикстура)


async def test_parallel_ticks_do_not_process_twice(env):  # noqa: F811
    await env.ingest(incoming("m1", "Сколько стоит монтаж?"))
    env.clock.advance(10)
    processed = await asyncio.gather(env.worker.tick(), env.worker.tick(), env.worker.tick())
    assert sum(processed) == 1
    assert len(env.fake.list_notes("leads", 1234)) == 1


async def test_late_client_message_after_manager_reply(env):  # noqa: F811
    """Повторная доставка старого сообщения клиента уже после ответа менеджера: режим — upsell_only."""
    await env.ingest(incoming("m1", "Здравствуйте", at=T0))
    await env.ingest(outgoing("o1", "Добрый день! Чем помочь?", at=T0 + timedelta(seconds=20)))
    await env.ingest(incoming("m0", "Сколько стоит монтаж?", at=T0 + timedelta(seconds=10)))
    env.clock.advance(10)
    assert await env.worker.tick() == 1
    assert env.llm.calls[0].mode == "upsell_only"


async def test_many_dialogs_respect_concurrency(env):  # noqa: F811
    for i in range(10):
        await env.ingest(incoming(f"m{i}", "Сколько стоит монтаж?", chat=f"chat-{i}"))
    env.clock.advance(10)
    total = 0
    for _ in range(5):
        total += await env.worker.tick()  # не больше WORKER_CONCURRENCY (3) за проход
    assert total == 10
    assert len(env.fake.list_notes("leads", 1234)) == 10


@pytest.mark.xfail(strict=True, reason="WORKER_CONCURRENCY=0 принимается — очередь молча не работает")
def test_zero_concurrency_is_rejected(tmp_path):
    with pytest.raises(ValidationError):
        make_settings(tmp_path, worker_concurrency=0)


@pytest.mark.xfail(strict=True, reason="отрицательные DEBOUNCE_SECONDS и RETENTION_DAYS принимаются")
def test_negative_timings_are_rejected(tmp_path):
    with pytest.raises(ValidationError):
        make_settings(tmp_path, debounce_seconds=-5, retention_days=-1)


# ---------- Лента страницы «amoCRM (mock)» ----------


@pytest.fixture
def page(tmp_path):
    settings = make_settings(
        tmp_path, amocrm_mode="mock", webhook_secret="s", debounce_seconds=0, worker_enabled=False
    )
    with TestClient(create_app(settings, llm=FakeLLM(make_suggestion()))) as client:
        yield client


def send(client, chat, text, direction="in", lead=1234):
    response = client.post(
        "/api/v1/amocrm-mock/messages",
        json={"lead_id": lead, "chat_id": chat, "text": text, "direction": direction},
    )
    assert response.status_code == 200, response.text


def tick(client):
    return client.portal.call(client.app.state.worker.tick)


def feed(client, chat, lead=1234):
    return client.get("/api/v1/amocrm-mock/feed", params={"lead_id": lead, "chat_id": chat}).json()


def test_feed_shows_only_notes_of_its_chat(page):
    # Два зрителя открыли одну сделку (на Vercel это один экземпляр функции).
    send(page, "web-alice", "Сколько стоит монтаж?")
    send(page, "web-bob", "А доставка сколько стоит?")
    assert tick(page) == 2
    alice = [item for item in feed(page, "web-alice")["items"] if item["kind"] == "note"]
    bob = [item for item in feed(page, "web-bob")["items"] if item["kind"] == "note"]
    assert len(alice) == len(bob) == 1 and alice[0]["id"] != bob[0]["id"]
    assert feed(page, "web-carol")["items"] == []  # новый чат той же сделки начинается с пустой ленты


async def test_failure_note_belongs_to_its_dialog(env):  # noqa: F811
    env.make_worker(FakeLLM(LLMRefusedError("отказ")))
    await env.ingest(incoming("m1", "…"))
    env.clock.advance(6)
    await env.worker.tick()
    [note] = env.fake.notes
    dialog = await env.worker.dialogs.get_by_key("chat-1")
    assert await env.worker.dialogs.note_ids(dialog.id) == {note["id"]}


async def test_recycled_note_id_returns_latest_suggestion(env):  # noqa: F811
    # Поддельный amoCRM после перезапуска нумерует примечания заново, а БД сервиса остаётся.
    for text in ("Сколько стоит монтаж?", "А доставка?"):
        await env.ingest(incoming(f"m-{text}", text, chat=f"chat-{text}"))
    env.clock.advance(6)
    await env.worker.tick()
    first, second = env.fake.notes
    saved_second = await env.suggestions.get_by_note_id(second["id"])
    await env.suggestions.set_note_id(saved_second["id"], first["id"])
    assert (await env.suggestions.get_by_note_id(first["id"]))["id"] == saved_second["id"]


def test_feed_queue_lifecycle(page):
    send(page, "web-1", "Сколько стоит монтаж?")
    assert feed(page, "web-1")["queue"]["status"] == "pending"
    tick(page)
    data = feed(page, "web-1")
    assert data["queue"]["status"] == "done"
    assert [item["kind"] for item in data["items"]] == ["message", "note"]


def test_unknown_chat_lead_and_bad_chat_id(page):
    assert feed(page, "nope", lead=999999)["items"] == []
    missing = page.post("/api/v1/amocrm-mock/messages", json={"lead_id": 999999, "chat_id": "c", "text": "x"})
    assert missing.status_code == 404
    bad = page.post("/api/v1/amocrm-mock/messages", json={"lead_id": 1234, "chat_id": "a b/c", "text": "x"})
    assert bad.status_code == 422


def test_manager_reply_before_processing_gives_upsell_only_note(page):
    send(page, "web-2", "Сколько стоит монтаж?")
    send(page, "web-2", "Монтаж — 9 900 ₽", direction="out")
    tick(page)
    [note] = [item for item in feed(page, "web-2")["items"] if item["kind"] == "note"]
    assert note["text"].startswith("Менеджер уже ответил клиенту")
    assert note["draft"] is None
