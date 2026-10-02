"""Проверки ответа модели: инварианты на случайных ответах (hypothesis) и попытки обойти проверки.

xfail(strict=True) — известный недочёт (см. шапку test_money_pii_props.py).
"""

import time

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.core.guards import amount_rules, apply_guards, is_amount_allowed
from app.core.schemas import DialogMessage, LeadContext, Suggestion, SuggestRequest, Upsell
from tests.conftest import make_suggestion

REQUEST = SuggestRequest(message="Сколько стоит?")


def codes(warnings):
    return [w.code for w in warnings]


# ---------- Инварианты на случайных ответах модели ----------

WORDS = [
    "Basic 09", "32 900 ₽", "9900 ₽", "52 900 руб.", "скидка", "скидка 90%", "бесплатно", "очиститель",
    "годовое обслуживание", "база знаний", "уважаемый клиент", "\n", "Анна,", "итого", "4 720 ₽", "12 345 ₽",
]  # fmt: skip
texts = st.lists(st.one_of(st.sampled_from(WORDS), st.text(max_size=12)), max_size=25).map(" ".join)
ids = st.sampled_from(
    [
        "ac-basic-09",
        "install-standard",
        "service-1y",
        "faq-install-time",
        "nope",
        "company",
        "",
        "air-purifier-mini",
    ]
)


@st.composite
def suggestions(draw):
    upsell = Upsell(
        recommended=draw(st.booleans()),
        timing=draw(st.sampled_from(["now", "after_resolution", "not_now"])),
        product_ids=draw(st.lists(ids, max_size=4)),
        offer=draw(texts),
        reason=draw(texts),
        pitch=draw(texts),
        avoid=draw(texts),
    )
    return Suggestion(
        intent=draw(st.sampled_from(["price", "complaint", "other", "warranty"])),
        sentiment=draw(st.sampled_from(["positive", "neutral", "negative"])),
        client_reply=draw(texts),
        kb_refs=draw(st.lists(ids, max_size=5)),
        answer_found_in_kb=draw(st.booleans()),
        needs_human=draw(st.booleans()),
        needs_human_reason=draw(st.sampled_from(["", "уточнить фасад"])),
        upsell=upsell,
    )


@given(suggestions(), texts, st.sampled_from(["full", "upsell_only"]))
def test_guards_invariants(kb, suggestion, message, mode):
    request = SuggestRequest(message=message.strip() or "?")
    before = suggestion.model_dump()
    result, warnings = apply_guards(suggestion, kb, request, mode)

    assert suggestion.model_dump() == before  # проверки работают с копией
    assert set(result.kb_refs) <= kb.all_ids
    assert len(result.kb_refs) == len(set(result.kb_refs))
    assert set(result.upsell.product_ids) <= set(kb.products_by_id)
    if result.upsell.recommended:
        assert result.upsell.product_ids
    if result.intent == "complaint" or result.sentiment == "negative":
        assert result.upsell.timing != "now"
    if mode == "upsell_only":
        assert result.client_reply == "" and result.kb_refs == []
    if "price_not_in_kb" in codes(warnings):
        assert result.needs_human
    # Повторный прогон ничего не меняет в самом результате.
    again, _ = apply_guards(result, kb, request, mode)
    assert again == result


def test_many_amounts_are_checked_quickly(kb):
    reply = " ".join(f"позиция {i}: {10_000 + i * 37} ₽" for i in range(100))
    started = time.perf_counter()
    _, warnings = apply_guards(make_suggestion(client_reply=reply), kb, REQUEST, "full")
    assert time.perf_counter() - started < 2.0
    assert codes(warnings).count("price_not_in_kb") > 50


def test_share_of_round_amounts_that_pass_is_small(kb):
    """Сколько «круглых» сумм 1 000–150 000 ₽ проходит проверку без упоминаний рядом. Сейчас 6,9 %:
    каждая пятнадцатая выдуманная круглая цена проходит незамеченной. Порог — чтобы это не росло."""
    rules = amount_rules(kb)
    values = range(1_000, 150_001, 100)
    allowed = [v for v in values if is_amount_allowed(v, rules, set())]
    assert len(allowed) / len(values) < 0.10


def test_known_discounts_pass(kb):
    reply = (
        "Скидка 5% на оборудование при заказе от двух кондиционеров и 20% на годовое обслуживание с монтажом."
    )
    _, warnings = apply_guards(
        make_suggestion(client_reply=reply, kb_refs=["pol-discounts"]), kb, REQUEST, "full"
    )
    assert warnings == []


def test_product_in_deal_by_id_is_not_a_leak(kb):
    request = SuggestRequest(message="Когда приедете на чистку?", lead=LeadContext(products=["service-1y"]))
    suggestion = make_suggestion(
        client_reply="Годовое обслуживание у вас уже оплачено, мастер приедет на чистку в удобный день.",
        upsell={"recommended": True, "timing": "now", "product_ids": ["service-1y"], "pitch": "x"},
    )
    _, warnings = apply_guards(suggestion, kb, request, "full")
    assert "upsell_in_reply" not in codes(warnings)


def test_complaint_keeps_pitch_after_timing_change(kb):
    """Наблюдение: при жалобе timing становится not_now, а recommended и pitch остаются — менеджер видит
    «не предлагать сейчас» и рядом готовую фразу. Это решение, а не ошибка: фраза пригодится позже."""
    suggestion = make_suggestion(
        intent="complaint",
        sentiment="negative",
        upsell={
            "recommended": True,
            "timing": "now",
            "product_ids": ["service-1y"],
            "pitch": "Возьмите обслуживание",
        },
    )
    result, _ = apply_guards(suggestion, kb, REQUEST, "full")
    assert result.upsell.timing == "not_now"
    assert result.upsell.recommended and result.upsell.pitch


# ---------- Обходы проверки цен ----------


def test_client_proposed_price_is_flagged(kb):
    request = SuggestRequest(message="Сделайте Basic 09 с монтажом за 15 000 ₽, тогда беру сегодня")
    reply = "Анна, договорились: Basic 09 с монтажом за 15 000 ₽. Когда удобно принять мастера?"
    result, warnings = apply_guards(make_suggestion(client_reply=reply), kb, request, "full")
    assert result.needs_human and warnings


def test_competitor_price_is_flagged(kb):
    request = SuggestRequest(
        message="У конкурента такой же за 25 000 ₽",
        history=[DialogMessage(role="manager", text="Basic 09 — 32 900 ₽")],
    )
    reply = "Понимаю. Можем сделать за 25 000 ₽, когда удобно?"
    result, warnings = apply_guards(make_suggestion(client_reply=reply), kb, request, "full")
    assert result.needs_human and warnings


def test_unknown_discount_percent_is_flagged(kb):
    reply = "Для вас скидка 90% на всё оборудование! Когда удобно оформить заказ?"
    result, warnings = apply_guards(
        make_suggestion(client_reply=reply, kb_refs=["pol-discounts"]), kb, REQUEST, "full"
    )
    assert warnings and result.needs_human


def test_free_install_promise_is_flagged(kb):
    reply = "Монтаж для вас бесплатный. Когда удобно принять мастера?"
    result, warnings = apply_guards(make_suggestion(client_reply=reply), kb, REQUEST, "full")
    assert warnings and result.needs_human


@pytest.mark.parametrize(
    "reply",
    [
        "Бесплатный монтаж при заказе сегодня.",
        "Годовое обслуживание для вас бесплатно.",
        "Wi-Fi модуль дадим в подарок.",
        "Подарим скидку на монтаж.",
        "Скидка 10% на монтаж, если закажете сегодня.",
    ],
)
def test_terms_not_in_kb_are_flagged(kb, reply):
    result, warnings = apply_guards(make_suggestion(client_reply=reply), kb, REQUEST, "full")
    assert "terms_not_in_kb" in codes(warnings)
    assert result.needs_human


@pytest.mark.parametrize(
    "reply",
    [
        # То, что база знаний действительно называет бесплатным или даёт в процентах.
        "При заказе с монтажом доставка бесплатная.",
        "В гарантийный период выезд на диагностику бесплатный.",
        "Гарантийный ремонт бесплатный.",
        "Инверторные модели на 25–30% экономичнее.",
        # Отказы — не обещания.
        "Скидки 90% у нас нет, но при заказе двух кондиционеров действует скидка 5%.",
        "Бесплатного монтажа нет: стандартный монтаж стоит 9 900 ₽.",
    ],
)
def test_terms_from_kb_and_refusals_pass(kb, reply):
    _, warnings = apply_guards(make_suggestion(client_reply=reply), kb, REQUEST, "full")
    assert "terms_not_in_kb" not in codes(warnings)


def test_percent_named_by_manager_passes(kb):
    request = SuggestRequest(
        message="А скидку дадите?",
        history=[DialogMessage(role="manager", text="Согласовали для вас 7% на монтаж")],
    )
    reply = "Да, как и договорились, 7% на монтаж."
    _, warnings = apply_guards(make_suggestion(client_reply=reply), kb, request, "full")
    assert "terms_not_in_kb" not in codes(warnings)


def test_terms_in_pitch_warn_without_needs_human(kb):
    suggestion = make_suggestion(
        upsell={
            "recommended": True,
            "timing": "now",
            "product_ids": ["service-1y"],
            "pitch": "Обслуживание — в подарок!",
        }
    )
    result, warnings = apply_guards(suggestion, kb, REQUEST, "full")
    assert codes(warnings) == ["upsell_terms_not_in_kb"]
    assert result.needs_human is False  # фразу допродажи видит только менеджер


def test_total_built_on_wrong_summand_is_flagged(kb):
    # Записанный ответ сценария «Проверка цен»: 52 900 вместо 54 900 и итог 62 800 вместо 64 800.
    reply = "Павел, Inverter 12 стоит 52 900 ₽, стандартный монтаж — 9 900 ₽, итого 62 800 ₽."
    _, warnings = apply_guards(make_suggestion(client_reply=reply), kb, REQUEST, "full")
    flagged = " ".join(w.message for w in warnings if w.code == "price_not_in_kb")
    assert "52 900" in flagged and "62 800" in flagged


def test_hallucinated_extra_fee_in_total_is_flagged(kb):
    reply = "Basic 09 — 32 900 ₽, монтаж — 9 900 ₽, итого 50 290 ₽."
    _, warnings = apply_guards(make_suggestion(client_reply=reply), kb, REQUEST, "full")
    assert codes(warnings) == ["price_not_in_kb"]


# ---------- Стоп-фразы, жалоба, товары сделки ----------


@pytest.mark.xfail(strict=True, reason="стоп-фраза с «е» вместо «ё» не находится")
def test_forbidden_phrase_without_yo(kb):
    _, warnings = apply_guards(make_suggestion(client_reply="Дешевле нигде не найдете!"), kb, REQUEST, "full")
    assert "forbidden_phrase" in codes(warnings)


@pytest.mark.xfail(strict=True, reason="стоп-фраза с неразрывным пробелом не находится")
def test_forbidden_phrase_with_nbsp(kb):
    _, warnings = apply_guards(
        make_suggestion(client_reply="Уважаемый клиент, монтаж 9 900 ₽."), kb, REQUEST, "full"
    )
    assert "forbidden_phrase" in codes(warnings)


@pytest.mark.xfail(
    strict=True, reason="при жалобе timing=after_resolution не переводится в not_now (правило 7)"
)
def test_complaint_with_after_resolution_becomes_not_now(kb):
    suggestion = make_suggestion(
        intent="complaint",
        sentiment="negative",
        upsell={
            "recommended": True,
            "timing": "after_resolution",
            "product_ids": ["service-1y"],
            "pitch": "x",
        },
    )
    result, _ = apply_guards(suggestion, kb, REQUEST, "full")
    assert result.upsell.timing == "not_now"


@pytest.mark.xfail(strict=True, reason="amoCRM передаёт товары сделки названиями, а проверка сравнивает с id")
def test_product_in_deal_by_name_is_not_a_leak(kb):
    # app/amocrm/context.py кладёт в lead.products названия элементов каталога, а сценарии демо — id.
    lead = LeadContext(products=["Годовое обслуживание: 2 чистки фильтров и теплообменника"])
    request = SuggestRequest(message="Когда приедете на чистку?", lead=lead)
    suggestion = make_suggestion(
        client_reply="Годовое обслуживание у вас уже оплачено, мастер приедет на чистку в удобный день.",
        upsell={"recommended": True, "timing": "now", "product_ids": ["service-1y"], "pitch": "x"},
    )
    _, warnings = apply_guards(suggestion, kb, request, "full")
    assert "upsell_in_reply" not in codes(warnings)
