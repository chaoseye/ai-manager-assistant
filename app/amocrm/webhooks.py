"""Разбор вебхуков amoCRM о сообщениях в чатах.

amoCRM присылает x-www-form-urlencoded с вложенными ключами в стиле PHP:
    message[add][0][text]=Привет&message[add][0][author][name]=Иван&account[id]=123
Входящие сообщения клиента лежат в message[add], исходящие (менеджер, бот) — в outgoing_message[add].
На всякий случай поддерживается и JSON с той же структурой.
"""

import json
import logging
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import parse_qsl

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

ELEMENT_CONTACT = 1
ELEMENT_LEAD = 2

_KEY_RE = re.compile(r"^([^\[\]]+)((?:\[[^\[\]]*\])*)$")
_PART_RE = re.compile(r"\[([^\[\]]*)\]")


class ChatMessageEvent(BaseModel):
    id: str
    direction: Literal["in", "out"]
    chat_id: str | None = None
    talk_id: str | None = None
    contact_id: int | None = None
    element_id: int | None = None
    element_type: int | None = None
    author_type: Literal["contact", "user", "bot"]
    author_name: str | None = None
    author_user_id: int | None = None
    text: str = ""
    attachment_type: str | None = None
    origin: str | None = None
    created_at: datetime

    @property
    def dialog_key(self) -> str:
        """Ключ диалога: чат, а если его нет — беседа или контакт."""
        if self.chat_id:
            return self.chat_id
        if self.talk_id:
            return f"talk-{self.talk_id}"
        return f"contact-{self.contact_id}"


class WebhookBatch(BaseModel):
    account_id: int | None = None
    subdomain: str | None = None
    messages: list[ChatMessageEvent] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)


class WebhookFormatError(ValueError):
    pass


# ---------- Вложенные ключи ----------


def _split_key(key: str) -> list[str]:
    match = _KEY_RE.match(key)
    if not match:
        return [key]
    return [match.group(1), *_PART_RE.findall(match.group(2))]


def parse_nested_form(pairs: Iterable[tuple[str, str]]) -> dict[str, Any]:
    """[("a[b][0][c]", "v")] → {"a": {"b": {"0": {"c": "v"}}}}."""
    root: dict[str, Any] = {}
    for key, value in pairs:
        parts = _split_key(key)
        node = root
        for part in parts[:-1]:
            child = node.get(part)
            if not isinstance(child, dict):
                child = {}
                node[part] = child
            node = child
        last = parts[-1]
        if last == "":  # a[]=1&a[]=2
            last = str(len(node))
        node[last] = value
    return root


def encode_nested_form(data: Any, prefix: str = "") -> list[tuple[str, str]]:
    """Обратное к parse_nested_form: словари и списки → плоские пары с ключами в скобках."""
    pairs: list[tuple[str, str]] = []
    if isinstance(data, dict):
        items = data.items()
    elif isinstance(data, list):
        items = ((str(i), v) for i, v in enumerate(data))
    else:
        return [(prefix, "" if data is None else str(data))]
    for key, value in items:
        full_key = f"{prefix}[{key}]" if prefix else str(key)
        pairs.extend(encode_nested_form(value, full_key))
    return pairs


def _items(value: Any) -> list[Any]:
    """Список из JSON или словарь {"0": …, "1": …} из формы — в порядке индексов."""
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        numeric = [k for k in value if str(k).isdigit()]
        return [value[k] for k in sorted(numeric, key=int)]
    return []


# Целые поля пишутся в SQLite (INTEGER — 64 бита со знаком): число длиннее не id, а мусор, и запись упала бы.
_INT_MAX = 2**63 - 1
# Unix-время в секундах до 5138 года. Больше — миллисекунды (их шлют некоторые интеграции) или мусор.
_MAX_UNIX_SECONDS = 10**11


def _to_int(value: Any) -> int | None:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if -_INT_MAX - 1 <= number <= _INT_MAX else None


def _to_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _to_datetime(value: Any) -> datetime:
    timestamp = _to_int(value)
    if timestamp is not None and timestamp > _MAX_UNIX_SECONDS:
        timestamp //= 1000
    if timestamp is None or timestamp <= 0 or timestamp > _MAX_UNIX_SECONDS:
        return datetime.now(UTC)
    try:
        return datetime.fromtimestamp(timestamp, UTC)
    except (OverflowError, OSError, ValueError):  # на Windows — уже после 3000 года
        return datetime.now(UTC)


# ---------- События ----------


def _to_event(item: Any, direction: Literal["in", "out"]) -> ChatMessageEvent | str:
    """Событие или строка с причиной, почему элемент пропущен."""
    if not isinstance(item, dict):
        return "элемент не является объектом"
    message_id = _to_str(item.get("id"))
    if not message_id:
        return "нет id сообщения"
    author = item.get("author") if isinstance(item.get("author"), dict) else {}
    user_id = _to_int(author.get("user_id"))
    if direction == "in":
        author_type: Literal["contact", "user", "bot"] = "contact"
    else:
        # Исходящее с user_id — от сотрудника; без него — от бота или интеграции.
        author_type = "user" if user_id else "bot"
    attachment = item.get("attachment") if isinstance(item.get("attachment"), dict) else {}
    event = ChatMessageEvent(
        id=message_id,
        direction=direction,
        chat_id=_to_str(item.get("chat_id")),
        talk_id=_to_str(item.get("talk_id")),
        contact_id=_to_int(item.get("contact_id")),
        element_id=_to_int(item.get("element_id")),
        element_type=_to_int(item.get("element_type")),
        author_type=author_type,
        author_name=_to_str(author.get("name")),
        author_user_id=user_id,
        text=str(item.get("text") or "").strip(),
        attachment_type=_to_str(attachment.get("type")),
        origin=_to_str(item.get("origin")),
        created_at=_to_datetime(item.get("created_at")),
    )
    if not (event.chat_id or event.talk_id or event.contact_id):
        return f"сообщение {message_id}: нет chat_id, talk_id и contact_id"
    return event


def parse_webhook(body: bytes, content_type: str | None) -> WebhookBatch:
    """Разбирает тело вебхука. События других типов (сделки, задачи) молча игнорируются."""
    try:
        if content_type and "json" in content_type.lower():
            data = json.loads(body.decode("utf-8") or "{}")
        else:
            data = parse_nested_form(parse_qsl(body.decode("utf-8"), keep_blank_values=True))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WebhookFormatError(f"не удалось разобрать тело вебхука: {exc}") from exc
    if not isinstance(data, dict):
        raise WebhookFormatError("тело вебхука должно быть объектом")

    account = data.get("account") if isinstance(data.get("account"), dict) else {}
    batch = WebhookBatch(account_id=_to_int(account.get("id")), subdomain=_to_str(account.get("subdomain")))
    for key, direction in (("message", "in"), ("outgoing_message", "out")):
        section = data.get(key)
        if not isinstance(section, dict):
            continue
        for item in _items(section.get("add")):
            result = _to_event(item, direction)  # type: ignore[arg-type]
            if isinstance(result, str):
                batch.skipped.append(result)
            else:
                batch.messages.append(result)
    batch.messages.sort(key=lambda m: m.created_at)
    return batch
