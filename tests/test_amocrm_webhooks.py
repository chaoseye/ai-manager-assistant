"""Разбор вебхуков: примеры построены по документации amoCRM (форма и JSON)."""

from datetime import UTC, datetime
from urllib.parse import urlencode

import pytest

from app.amocrm.webhooks import (
    WebhookFormatError,
    encode_nested_form,
    parse_nested_form,
    parse_webhook,
)

INCOMING = {
    "id": "amo12345-31ed-41af-am23-conf1504",
    "chat_id": "2f61amo-c914-r429-m4f1-4005c15o5n0f",
    "talk_id": "117",
    "contact_id": "3372695",
    "author": {"id": "7a389amo2", "type": "external", "name": "Ivan Ivanov", "avatar_url": "https://x"},
    "text": "Hello World!",
    "created_at": "1580116931",
    "message_type": "text",
    "origin": "telegram",
    "element_id": "123456789",
    "element_type": "2",
}
OUTGOING = {
    "id": "conf1504-41af-am23-31ed-amo12345",
    "chat_id": "2f61amo-c914-r429-m4f1-4005c15o5n0f",
    "talk_id": "117",
    "contact_id": "3372695",
    "text": "Здравствуйте! Чем могу помочь?",
    "created_at": "1580116990",
    "type": "outgoing",
    "origin": "telegram",
    "author": {"id": "8216amo2", "user_id": "123123", "type": "internal", "name": "Ольга"},
    "recipient": {"id": "7a389amo2", "type": "external", "name": "Ivan Ivanov"},
}
ACCOUNT = {"id": "29000001", "subdomain": "klimat-demo"}


def form_body(data: dict) -> bytes:
    return urlencode(encode_nested_form(data)).encode()


def test_nested_form_roundtrip():
    data = {"message": {"add": [INCOMING]}, "account": ACCOUNT}
    pairs = encode_nested_form(data)
    assert ("message[add][0][author][name]", "Ivan Ivanov") in pairs
    parsed = parse_nested_form(pairs)
    assert parsed["message"]["add"]["0"]["author"]["name"] == "Ivan Ivanov"
    assert parsed["account"]["subdomain"] == "klimat-demo"
    assert parse_nested_form([("a[]", "1"), ("a[]", "2")]) == {"a": {"0": "1", "1": "2"}}


def test_incoming_and_outgoing_form_payload():
    body = form_body(
        {"message": {"add": [INCOMING]}, "outgoing_message": {"add": [OUTGOING]}, "account": ACCOUNT}
    )
    batch = parse_webhook(body, "application/x-www-form-urlencoded")
    assert batch.account_id == 29000001 and batch.subdomain == "klimat-demo"
    assert [m.direction for m in batch.messages] == ["in", "out"]  # отсортированы по времени

    incoming, outgoing = batch.messages
    assert incoming.author_type == "contact"
    assert incoming.author_name == "Ivan Ivanov"
    assert incoming.text == "Hello World!"
    assert incoming.element_type == 2 and incoming.element_id == 123456789
    assert incoming.contact_id == 3372695
    assert incoming.created_at == datetime(2020, 1, 27, 9, 22, 11, tzinfo=UTC)
    assert incoming.dialog_key == INCOMING["chat_id"]

    assert outgoing.author_type == "user"
    assert outgoing.author_user_id == 123123
    assert outgoing.author_name == "Ольга"


def test_outgoing_without_user_is_bot():
    bot = {**OUTGOING, "author": {"id": "bot", "type": "bot", "name": "Salesbot"}}
    batch = parse_webhook(form_body({"outgoing_message": {"add": [bot]}}), None)
    assert batch.messages[0].author_type == "bot"


def test_json_payload_and_attachment():
    import json

    picture = {
        **INCOMING,
        "text": "",
        "attachment": {"type": "picture", "link": "https://x", "file_name": "a.gif"},
    }
    batch = parse_webhook(json.dumps({"message": {"add": [picture]}}).encode(), "application/json")
    assert batch.messages[0].text == ""
    assert batch.messages[0].attachment_type == "picture"


def test_multiple_messages_are_ordered_by_time():
    later = {**INCOMING, "id": "m2", "created_at": "1580117000", "text": "второе"}
    earlier = {**INCOMING, "id": "m1", "created_at": "1580116000", "text": "первое"}
    batch = parse_webhook(form_body({"message": {"add": [later, earlier]}}), None)
    assert [m.text for m in batch.messages] == ["первое", "второе"]


def test_other_events_are_ignored_and_broken_items_skipped():
    no_id = {**INCOMING, "id": ""}
    no_dialog = {**INCOMING, "id": "x", "chat_id": "", "talk_id": "", "contact_id": ""}
    body = form_body({"leads": {"status": [{"id": "1"}]}, "message": {"add": [no_id, no_dialog]}})
    batch = parse_webhook(body, None)
    assert batch.messages == []
    assert len(batch.skipped) == 2


def test_dialog_key_fallbacks():
    no_chat = {**INCOMING, "chat_id": ""}
    assert (
        parse_webhook(form_body({"message": {"add": [no_chat]}}), None).messages[0].dialog_key == "talk-117"
    )
    only_contact = {**INCOMING, "chat_id": "", "talk_id": ""}
    event = parse_webhook(form_body({"message": {"add": [only_contact]}}), None).messages[0]
    assert event.dialog_key == "contact-3372695"


def test_bad_bodies():
    with pytest.raises(WebhookFormatError):
        parse_webhook(b"{not json", "application/json")
    with pytest.raises(WebhookFormatError):
        parse_webhook(b"[1, 2]", "application/json")
    assert parse_webhook(b"", None).messages == []
