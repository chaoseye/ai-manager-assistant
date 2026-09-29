"""AmoClient против поддельного amoCRM и против собранных вручную ответов (ошибки, повторы)."""

import httpx
import pytest

from app.amocrm.client import AmoAuthError, AmoClient, AmoRequestError, AmoUnavailableError
from app.amocrm.context import pick_lead, resolve_context
from app.amocrm.fake import FakeAmoApi
from app.config import BASE_DIR

SEED = BASE_DIR / "examples" / "amocrm" / "mock_account.json"


@pytest.fixture
def fake() -> FakeAmoApi:
    return FakeAmoApi.from_file(SEED, token="t0ken")


def make_client(transport: httpx.AsyncBaseTransport, token: str = "t0ken", **kwargs) -> AmoClient:
    return AmoClient(
        "https://klimat-demo.amocrm.ru", token, rps=1000, backoff_seconds=0, transport=transport, **kwargs
    )


async def test_read_methods(fake):
    amo = make_client(fake.transport())
    assert (await amo.get_account())["subdomain"] == "klimat-demo"

    lead = await amo.get_lead(1236)
    assert lead.name == "Basic 09 с монтажом" and lead.price == 42800
    assert lead.main_contact_id == 3001236
    assert lead.catalog_elements == ((5001, 90002), (5001, 90010))
    assert lead.tags == ("telegram",)
    assert await amo.get_lead(999999) is None  # amoCRM отвечает 204

    contact = await amo.get_contact(3001234, with_leads=True)
    assert contact.name == "Анна" and contact.lead_ids == (1234, 1238)
    assert await amo.get_contact(1) is None

    leads = await amo.get_leads([1234, 1238, 424242])
    assert sorted(lead.id for lead in leads) == [1234, 1238]
    assert await amo.get_leads([424242]) == []

    pipelines = await amo.get_pipelines()
    assert pipelines[7000001].statuses[142] == "Успешно реализовано"
    names = await amo.get_catalog_element_names(5001, [90002, 90010])
    assert names == {90002: "Сплит-система Basic 09", 90010: "Стандартный монтаж"}
    await amo.aclose()


async def test_caches(fake):
    amo = make_client(fake.transport())
    await amo.get_pipelines()
    await amo.get_pipelines()
    await amo.get_catalog_element_names(5001, [90002])
    await amo.get_catalog_element_names(5001, [90002])
    paths = [p for _, p in fake.requests]
    assert paths.count("/api/v4/leads/pipelines") == 1
    assert paths.count("/api/v4/catalogs/5001/elements") == 1


async def test_add_note_and_webhook(fake):
    amo = make_client(fake.transport())
    note_id = await amo.add_note("leads", 1234, "Текст", "AI-помощник")
    assert note_id == fake.notes[0]["id"]
    assert fake.notes[0]["note_type"] == "service_message"
    assert fake.notes[0]["params"] == {"service": "AI-помощник", "text": "Текст"}
    with pytest.raises(AmoRequestError, match="400"):
        await amo.add_note("leads", 424242, "Текст", "AI-помощник")
    await amo.subscribe_webhook("https://x/webhooks/amocrm/s", ["add_message"])
    assert fake.webhooks == [{"destination": "https://x/webhooks/amocrm/s", "settings": ["add_message"]}]


async def test_wrong_token(fake):
    amo = make_client(fake.transport(), token="wrong")
    with pytest.raises(AmoAuthError):
        await amo.get_account()


async def test_retries_on_429_and_5xx_then_success():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if len(calls) == 1:
            return httpx.Response(429, json={"title": "Too Many Requests"})
        if len(calls) == 2:
            return httpx.Response(502)
        return httpx.Response(200, json={"id": 1})

    amo = make_client(httpx.MockTransport(handler))
    assert await amo.get_account() == {"id": 1}
    assert len(calls) == 3


async def test_gives_up_after_retries():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("нет сети", request=request)

    amo = make_client(httpx.MockTransport(handler), max_retries=2)
    with pytest.raises(AmoUnavailableError, match="ConnectError"):
        await amo.get_account()


async def test_client_errors_are_not_retried():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(402, json={"title": "Payment Required", "detail": "Подписка закончилась"})

    amo = make_client(httpx.MockTransport(handler))
    with pytest.raises(AmoRequestError, match="Подписка закончилась"):
        await amo.get_account()
    assert len(calls) == 1


async def test_rate_limiter_spaces_requests(fake):
    import time

    amo = AmoClient("https://x.amocrm.ru", "t0ken", rps=20, transport=fake.transport())
    started = time.monotonic()
    for _ in range(5):
        await amo.get_account()
    assert time.monotonic() - started >= 0.18  # 4 интервала по 0,05 с


# ---------- Контекст сделки ----------


async def test_context_from_lead(fake):
    amo = make_client(fake.transport())
    ctx = await resolve_context(
        amo, element_type=2, element_id=1236, contact_id=None, fallback_name="Irina T."
    )
    assert ctx.note_target == ("leads", 1236)
    lead = ctx.lead
    assert lead.id == 1236 and lead.pipeline == "Продажи" and lead.stage == "Монтаж назначен"
    assert lead.budget == 42800
    assert lead.products == ["Сплит-система Basic 09", "Стандартный монтаж"]
    assert lead.contact_name == "Ирина"  # имя из карточки контакта важнее имени из мессенджера


async def test_context_from_contact_prefers_open_lead(fake):
    amo = make_client(fake.transport())
    ctx = await resolve_context(amo, element_type=1, element_id=3001300, contact_id=None, fallback_name=None)
    assert ctx.lead.id == 1301  # открытая, хотя закрытая 1300 обновлялась позже
    assert ctx.lead.contact_name == "Олег"


async def test_context_closed_lead_and_no_leads(fake):
    amo = make_client(fake.transport())
    ctx = await resolve_context(
        amo, element_type=1, element_id=3001101, contact_id=3001101, fallback_name=None
    )
    assert ctx.lead.id == 1101 and ctx.lead.stage == "Успешно реализовано"

    no_leads = await resolve_context(
        amo, element_type=1, element_id=3001400, contact_id=None, fallback_name="Лена"
    )
    assert no_leads.lead is None and no_leads.note_target == ("contacts", 3001400)

    unknown = await resolve_context(amo, element_type=2, element_id=777, contact_id=None, fallback_name=None)
    assert unknown.lead is None and unknown.note_target is None


async def test_contact_without_name_uses_messenger_name(fake):
    amo = make_client(fake.transport())
    ctx = await resolve_context(amo, element_type=2, element_id=1239, contact_id=None, fallback_name="Гость")
    assert ctx.lead.contact_name == "Гость"
    assert ctx.lead.stage == "Неразобранное"


def test_pick_lead_empty():
    assert pick_lead([]) is None
