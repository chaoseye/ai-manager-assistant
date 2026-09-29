import pytest

from app.core.guards import amount_rules, apply_guards, is_amount_allowed
from app.core.schemas import DialogMessage, SuggestRequest
from tests.conftest import make_suggestion

REQUEST = SuggestRequest(message="Сколько стоит?")


def codes(warnings):
    return [w.code for w in warnings]


@pytest.mark.parametrize(
    ("value", "conversation", "nearby", "allowed"),
    [
        (32900, set(), set(), True),  # цена из БЗ
        (990, set(), set(), True),  # сумма из текста условий (доставка)
        (42800, set(), set(), True),  # кондиционер + монтаж
        (65800, set(), set(), True),  # две штуки
        (4720, set(), set(), True),  # годовое обслуживание со скидкой 20%
        (1180, set(), set(), True),  # размер скидки
        (57760, set(), set(), True),  # два кондиционера со скидкой 5%
        (33890, set(), set(), True),  # кондиционер + доставка
        (47520, set(), set(), False),  # три позиции без упоминания слагаемых
        (47520, set(), {32900, 9900, 4720}, True),  # …но со слагаемыми рядом — можно
        (47520, {32900}, {9900, 4720}, True),
        (32000, set(), set(), False),
        (31900, set(), set(), False),
        (45000, set(), set(), False),
        (31900, set(), {31900}, False),  # сумма не оправдывает сама себя
        (12345, {12345}, set(), True),  # сумму назвал клиент
    ],
)
def test_amount_rules(kb, value, conversation, nearby, allowed):
    assert is_amount_allowed(value, amount_rules(kb), conversation, nearby) is allowed


def test_clean_suggestion_passes_without_warnings(kb):
    suggestion = make_suggestion(
        client_reply="Basic 09 — 32 900 ₽, монтаж — 9 900 ₽, итого 42 800 ₽. Когда удобно?",
        kb_refs=["ac-basic-09", "install-standard"],
        upsell={
            "recommended": True,
            "timing": "now",
            "product_ids": ["service-1y"],
            "offer": "Обслуживание: 4 720 ₽ вместо 5 900 ₽",
            "reason": "Клиент покупает с монтажом.",
            "pitch": "С монтажом обслуживание — 4 720 ₽ вместо 5 900 ₽.",
        },
    )
    result, warnings = apply_guards(suggestion, kb, REQUEST, "full")
    assert warnings == []
    assert result == suggestion
    assert result is not suggestion  # исходный объект не меняется


def test_invented_price_sets_needs_human(kb):
    suggestion = make_suggestion(client_reply="Basic 09 стоит 31 900 ₽.", kb_refs=["ac-basic-09"])
    result, warnings = apply_guards(suggestion, kb, REQUEST, "full")
    assert codes(warnings) == ["price_not_in_kb"]
    assert "31 900 ₽" in warnings[0].message
    assert result.needs_human is True
    assert "Проверьте цены" in result.needs_human_reason
    assert suggestion.needs_human is False


def test_amount_named_by_client_is_allowed(kb):
    request = SuggestRequest(
        message="Бюджет у меня 40 000 ₽, уложимся?",
        history=[DialogMessage(role="manager", text="Могу предложить вариант за 36 800 ₽")],
    )
    suggestion = make_suggestion(client_reply="В 40 000 ₽ уложимся: вариант за 36 800 ₽.")
    _, warnings = apply_guards(suggestion, kb, request, "full")
    assert "price_not_in_kb" not in codes(warnings)


def test_upsell_price_is_checked_separately(kb):
    suggestion = make_suggestion(
        upsell={
            "recommended": True,
            "timing": "now",
            "product_ids": ["service-1y"],
            "offer": "Обслуживание",
            "reason": "…",
            "pitch": "Обслуживание всего за 2 222 ₽!",
        }
    )
    result, warnings = apply_guards(suggestion, kb, REQUEST, "full")
    assert codes(warnings) == ["upsell_price_not_in_kb"]
    assert result.needs_human is False  # подсказку видит только менеджер


def test_unknown_refs_and_products_are_removed(kb):
    suggestion = make_suggestion(
        kb_refs=["install-standard", "faq-teleport", "install-standard"],
        upsell={
            "recommended": True,
            "timing": "now",
            "product_ids": ["ac-quantum-99"],
            "offer": "Квантовый кондиционер",
            "reason": "…",
            "pitch": "…",
        },
    )
    result, warnings = apply_guards(suggestion, kb, REQUEST, "full")
    assert result.kb_refs == ["install-standard"]
    assert result.upsell.product_ids == []
    assert result.upsell.recommended is False
    assert codes(warnings) == ["unknown_kb_ref", "unknown_product", "upsell_without_products"]


def test_answer_found_without_refs(kb):
    suggestion = make_suggestion(kb_refs=[], answer_found_in_kb=True)
    _, warnings = apply_guards(suggestion, kb, REQUEST, "full")
    assert codes(warnings) == ["no_kb_refs"]


@pytest.mark.parametrize(("sentiment", "intent"), [("negative", "price"), ("neutral", "complaint")])
def test_no_upsell_now_on_complaint(kb, sentiment, intent):
    suggestion = make_suggestion(
        sentiment=sentiment,
        intent=intent,
        upsell={
            "recommended": True,
            "timing": "now",
            "product_ids": ["service-1y"],
            "offer": "Обслуживание",
            "reason": "…",
            "pitch": "",
        },
    )
    result, warnings = apply_guards(suggestion, kb, REQUEST, "full")
    assert result.upsell.timing == "not_now"
    assert codes(warnings) == ["upsell_on_complaint"]


def test_forbidden_phrases_and_length(kb):
    suggestion = make_suggestion(
        client_reply="Уважаемый клиент, гарантируем 100% качество. " + "Очень подробно. " * 70,
        upsell={"pitch": "У нас самая низкая цена"},
    )
    _, warnings = apply_guards(suggestion, kb, REQUEST, "full")
    assert codes(warnings).count("forbidden_phrase") == 3
    assert "reply_too_long" in codes(warnings)


def test_upsell_only_mode_clears_reply(kb):
    suggestion = make_suggestion(client_reply="Черновик, который не нужен", kb_refs=["install-standard"])
    result, warnings = apply_guards(suggestion, kb, REQUEST, "upsell_only")
    assert result.client_reply == ""
    assert result.kb_refs == []
    assert warnings == []


def test_amount_from_manager_reply_is_allowed(kb):
    request = SuggestRequest(message="?", replies=[DialogMessage(role="manager", text="Итого 12 345 ₽")])
    suggestion = make_suggestion(upsell={"pitch": "Как и говорили, 12 345 ₽"})
    _, warnings = apply_guards(suggestion, kb, request, "upsell_only")
    assert warnings == []
