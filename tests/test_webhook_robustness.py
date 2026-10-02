"""Вебхук amoCRM: разбор произвольного ввода, граничные значения полей, стоимость больших посылок.

amoCRM отключает хук после 100 ошибок за 2 часа, поэтому на то, что не разобралось, ответ — 200.
xfail(strict=True) — известный недочёт (см. шапку test_money_pii_props.py).
"""

import contextlib
import time
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient
from hypothesis import given
from hypothesis import strategies as st

from app.amocrm.webhooks import WebhookFormatError, encode_nested_form, parse_nested_form, parse_webhook
from app.main import create_app
from tests.conftest import FakeLLM, make_settings, make_suggestion

SECRET = "hook-secret"


def item(**overrides):
    base = {
        "id": "m1",
        "chat_id": "chat-42",
        "talk_id": "7",
        "contact_id": "3001234",
        "author": {"id": "x", "type": "external", "name": "Анна"},
        "text": "Сколько стоит монтаж?",
        "created_at": "1790600000",
        "origin": "telegram",
        "element_id": "1234",
        "element_type": "2",
    }
    base.update(overrides)
    return base


def body(*items, account=True):
    data: dict = {"message": {"add": list(items)}}
    if account:
        data["account"] = {"id": "29000001", "subdomain": "klimat-demo"}
    return urlencode(encode_nested_form(data)).encode()


@pytest.fixture
def hook(tmp_path):
    settings = make_settings(
        tmp_path,
        amocrm_mode="mock",
        webhook_secret=SECRET,
        debounce_seconds=0,
        worker_enabled=False,
        amocrm_account_id=29000001,
    )
    # raise_server_exceptions=False — смотрим на ответ так, как его увидит amoCRM.
    app = create_app(settings, llm=FakeLLM(make_suggestion()))
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def post(client, content: bytes, content_type="application/x-www-form-urlencoded"):
    return client.post(f"/webhooks/amocrm/{SECRET}", content=content, headers={"Content-Type": content_type})


# ---------- Разбор: только WebhookFormatError ----------


@given(
    st.binary(max_size=400), st.sampled_from([None, "application/json", "application/x-www-form-urlencoded"])
)
def test_parse_raises_only_format_error(raw, content_type):
    with contextlib.suppress(WebhookFormatError):
        parse_webhook(raw, content_type)


@given(
    st.lists(
        st.tuples(st.text(alphabet="abc[]0_", min_size=1, max_size=12), st.text(max_size=10)), max_size=20
    )
)
def test_nested_form_parser_never_crashes(pairs):
    parse_nested_form(pairs)


_tree = st.recursive(
    st.text(alphabet="абвxyz 0123", max_size=8),
    lambda children: st.dictionaries(
        st.text(alphabet="abcxyz_", min_size=1, max_size=6), children, max_size=4
    ),
    max_leaves=12,
)


def _prune(node):
    """Пустой словарь формой не передать — убираем такие ветки."""
    if not isinstance(node, dict):
        return node
    pruned = {key: _prune(value) for key, value in node.items()}
    return {key: value for key, value in pruned.items() if value != {}}


@given(st.dictionaries(st.text(alphabet="abcxyz_", min_size=1, max_size=6), _tree, max_size=4))
def test_encode_parse_roundtrip(data):
    data = _prune(data)
    assert parse_nested_form(encode_nested_form(data)) == data


# ---------- Граничные значения полей ----------


def test_created_at_in_milliseconds(hook):
    assert post(hook, body(item(created_at="1790600000000"))).status_code == 200


def test_created_at_in_milliseconds_keeps_the_time():
    in_seconds = parse_webhook(body(item(created_at="1790600000")), None).messages[0].created_at
    in_millis = parse_webhook(body(item(created_at="1790600000123")), None).messages[0].created_at
    assert in_millis == in_seconds


def test_huge_contact_id(hook):
    # Число длиннее 64 бит — не id: поле отбрасывается, сообщение с chat_id всё равно принимается.
    response = post(hook, body(item(contact_id="9" * 25)))
    assert response.status_code == 200 and response.json()["accepted"] == 1
    assert parse_webhook(body(item(contact_id="9" * 25)), None).messages[0].contact_id is None


def test_created_at_garbage(hook):
    for value in ("abc", "-5", "", "0"):
        assert post(hook, body(item(id=f"m-{value}", created_at=value))).status_code == 200


def test_missing_account_is_ignored_when_account_is_pinned(hook):
    assert post(hook, body(item(), account=False)).json()["accepted"] == 0


def test_non_object_bodies_are_ignored(hook):
    assert post(hook, b"[1,2,3]", "application/json").json() == {"ok": True, "accepted": 0}
    assert post(hook, b"\xff\xfe", "application/json").json() == {"ok": True, "accepted": 0}


def test_duplicate_inside_one_batch(hook):
    result = post(hook, body(item(id="dup"), item(id="dup"))).json()
    assert result["accepted"] == 1 and result["duplicates"] == 1


@pytest.mark.parametrize(
    "secret", ["%D1%81%D0%B5%D0%BA%D1%80%D0%B5%D1%82", "..%2F..%2Fhealth", "HOOK-SECRET"]
)
def test_wrong_secret_variants(hook, secret):
    assert hook.post(f"/webhooks/amocrm/{secret}", content=body(item())).status_code == 404


@pytest.mark.parametrize("size", [10, 100])
def test_batch_fits_amocrm_timeout(hook, size):
    items = [
        item(id=f"b{size}-{i}", chat_id=f"chat-{i % 7}", created_at=str(1790600000 + i)) for i in range(size)
    ]
    started = time.perf_counter()
    response = post(hook, body(*items))
    assert response.json()["accepted"] == size
    assert time.perf_counter() - started < 2.0  # amoCRM ждёт ответ не дольше 2 с
