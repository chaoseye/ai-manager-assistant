"""Поддельный amoCRM (только AMOCRM_MODE=mock): сделки, лента сделки и отправка сообщений «как из amoCRM».

Сообщение со страницы «amoCRM (mock)» превращается в тело вебхука того же формата, что шлёт amoCRM,
и проходит настоящий разбор и очередь. Секрет вебхука при этом в браузер не попадает.
"""

import time
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.amocrm.fake import FakeAmoApi
from app.amocrm.notes import REPLY_HEADER
from app.amocrm.payloads import FORM_CONTENT_TYPE, message_item, webhook_body
from app.amocrm.webhooks import parse_webhook
from app.storage.db import to_iso

router = APIRouter(prefix="/api/v1/amocrm-mock", tags=["amocrm-mock"])


def _fake(request: Request) -> FakeAmoApi:
    fake = getattr(request.app.state, "fake_amo", None)
    if fake is None:
        raise HTTPException(status_code=404, detail="Доступно только при AMOCRM_MODE=mock")
    return fake


@router.get("/notes", summary="Примечания, записанные в поддельный amoCRM")
async def notes(
    request: Request,
    entity_type: Literal["leads", "contacts"] | None = None,
    entity_id: int | None = None,
    after_id: int = 0,
) -> list[dict[str, Any]]:
    return _fake(request).list_notes(entity_type, entity_id, after_id)


@router.get("/leads", summary="Сделки поддельного аккаунта")
async def leads(request: Request) -> list[dict[str, Any]]:
    return _fake(request).list_leads()


class MockMessage(BaseModel):
    lead_id: int
    chat_id: str = Field(min_length=1, max_length=100, pattern=r"^[\w\-]+$")
    text: str = Field(min_length=1, max_length=4000)
    direction: Literal["in", "out"] = "in"
    author_name: str | None = Field(default=None, max_length=100)
    created_at: int | None = Field(default=None, description="Unix-время сообщения; по умолчанию — сейчас")


@router.post("/messages", summary="Отправить сообщение в сервис так, как его прислал бы amoCRM")
async def send_message(message: MockMessage, request: Request) -> dict[str, Any]:
    fake = _fake(request)
    lead = fake.leads.get(message.lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail=f"Сделки {message.lead_id} нет в поддельном аккаунте")
    contact_id = lead["contacts"][0] if lead.get("contacts") else None
    contact_name = fake.contacts.get(contact_id, {}).get("name") if contact_id else None
    default_name = (contact_name or "Клиент") if message.direction == "in" else "Менеджер"
    item = message_item(
        direction=message.direction,
        text=message.text.strip(),
        lead_id=message.lead_id,
        contact_id=contact_id,
        chat_id=message.chat_id,
        created_at=message.created_at or int(time.time()),
        author_name=message.author_name or default_name,
    )
    batch = parse_webhook(webhook_body(item, message.direction, fake.account), FORM_CONTENT_TYPE)
    result = await request.app.state.inbox.ingest(batch)
    return {"chat_id": message.chat_id, "message_id": item["id"], **result}


@router.get("/feed", summary="Лента сделки: сообщения чата, примечания AI-помощника, состояние очереди")
async def feed(request: Request, lead_id: int, chat_id: str, after_note_id: int = 0) -> dict[str, Any]:
    fake = _fake(request)
    state = request.app.state
    items: list[dict[str, Any]] = []
    queue: dict[str, Any] | None = None
    now = datetime.now(UTC)

    dialog = await state.dialogs.get_by_key(chat_id)
    if dialog is not None:
        for message in await state.dialogs.messages(dialog.id):
            items.append(
                {
                    "kind": "message",
                    "id": message.amo_id,
                    "direction": message.direction,
                    "author_type": message.author_type,
                    "author_name": message.author_name,
                    "text": message.text,
                    "attachment_type": message.attachment_type,
                    "at": to_iso(message.created_at),
                    "_sort": message.created_at.timestamp(),
                }
            )
        job = await state.jobs.latest_for_dialog(dialog.id)
        if job is not None:
            queue = {
                "status": job.status,
                "attempts": job.attempts,
                "error": job.error,
                "seconds_left": max(0.0, round((job.run_at - now).total_seconds(), 1)),
            }

    for note in fake.list_notes("leads", lead_id, after_note_id):
        saved = await state.repo.get_by_note_id(note["id"])
        suggestion = saved["suggestion"] if saved else None
        text = note["params"].get("text", "")
        has_draft = suggestion is not None and text.startswith(REPLY_HEADER)
        has_pitch = (
            suggestion is not None and suggestion["upsell"]["recommended"] and suggestion["upsell"]["pitch"]
        )
        items.append(
            {
                "kind": "note",
                "id": note["id"],
                "service": note["params"].get("service", ""),
                "text": text,
                "at": to_iso(datetime.fromtimestamp(note["created_at"], UTC)),
                "draft": suggestion["client_reply"] if has_draft else None,
                "pitch": suggestion["upsell"]["pitch"] if has_pitch else None,
                "_sort": float(note["created_at"])
                + 0.5,  # примечание появляется после сообщений той же секунды
            }
        )

    items.sort(key=lambda item: item["_sort"])
    for item in items:
        del item["_sort"]
    return {"items": items, "queue": queue, "debounce_seconds": state.settings.debounce_seconds}
