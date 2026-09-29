"""Сообщения «как из amoCRM»: собирают тело вебхука в том же формате, что шлёт amoCRM.

Используются имитатором (консоль) и страницей «amoCRM (mock)»: оба пропускают сообщения через
настоящий разбор вебхука и очередь, а не в обход них.
"""

import uuid
from typing import Any, Literal
from urllib.parse import urlencode

from app.amocrm.webhooks import ELEMENT_LEAD, encode_nested_form

MANAGER_USER_ID = 101
FORM_CONTENT_TYPE = "application/x-www-form-urlencoded"


def message_item(
    *,
    direction: Literal["in", "out"],
    text: str,
    lead_id: int,
    contact_id: int | None,
    chat_id: str,
    created_at: int,
    author_name: str,
    origin: str = "telegram",
) -> dict[str, Any]:
    """Одно сообщение в формате вебхука amoCRM (как в примерах документации)."""
    item: dict[str, Any] = {
        "id": f"sim-{uuid.uuid4()}",
        "chat_id": chat_id,
        "talk_id": str(lead_id),
        "contact_id": str(contact_id or ""),
        "text": text,
        "created_at": str(created_at),
        "origin": origin,
        "element_id": str(lead_id),
        "element_type": str(ELEMENT_LEAD),
    }
    if direction == "in":
        item["author"] = {"id": f"client-{contact_id}", "type": "external", "name": author_name}
    else:
        item["type"] = "outgoing"
        item["author"] = {
            "id": f"user-{MANAGER_USER_ID}",
            "user_id": str(MANAGER_USER_ID),
            "type": "internal",
            "name": author_name,
        }
    return item


def webhook_body(item: dict[str, Any], direction: Literal["in", "out"], account: dict[str, Any]) -> bytes:
    """Тело вебхука x-www-form-urlencoded с вложенными ключами: message[add][0][...] для клиента,
    outgoing_message[add][0][...] для менеджера."""
    key = "message" if direction == "in" else "outgoing_message"
    data = {
        key: {"add": [item]},
        "account": {"id": str(account.get("id", "")), "subdomain": account.get("subdomain", "")},
    }
    return urlencode(encode_nested_form(data)).encode()
