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
        (12345, {12345}, set(), True),  # сумму назвал менеджер
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


def test_invented_price_without_currency_is_caught(kb):
    # «₽» модель может и не написать — выдуманная цена всё равно не должна уйти клиенту без проверки.
    suggestion = make_suggestion(
        client_reply="Basic 09 стоит 31 900, монтаж — отдельно.", kb_refs=["ac-basic-09"]
    )
    result, warnings = apply_guards(suggestion, kb, REQUEST, "full")
    assert codes(warnings) == ["price_not_in_kb"]
    assert (warnings[0].field, warnings[0].fragment) == ("client_reply", "31 900")
    assert result.needs_human is True


def test_right_price_without_currency_passes(kb):
    suggestion = make_suggestion(
        client_reply="Basic 09 стоит 32 900, монтаж — 9 900 ₽.", kb_refs=["ac-basic-09"]
    )
    _, warnings = apply_guards(suggestion, kb, REQUEST, "full")
    assert warnings == []


@pytest.mark.parametrize(
    "reply",
    [
        "Да, сделаем за 30 000 ₽.",
        # Точка сокращения обрывала предложение, и сумма выпадала из проверки.
        "Да, сделаем за 30 000 р. Мастер приедет в течение 1–3 рабочих дней.",
        "Да, сделаем за 30 тыс. руб. Когда удобно принять мастера?",
        "Да, 30 000 р. за всё, мастер приедет завтра.",
        # Цена из БЗ рядом не делает сумму клиента ценой: её клиент назвал не бюджетом.
        "Договорились: 30 000 ₽ вместо 32 900 ₽.",
    ],
)
def test_amount_named_by_client_needs_a_check(kb, reply):
    # Сумму менеджера ответ повторяет свободно, сумму клиента — только с проверкой: «сделайте за 30 000 ₽» →
    # «договорились, 30 000 ₽» не должно уйти клиенту незамеченным.
    request = SuggestRequest(message="Сделаете Basic 09 с монтажом за 30 000 ₽?")
    result, warnings = apply_guards(make_suggestion(client_reply=reply), kb, request, "full")
    assert codes(warnings) == ["client_amount"]
    assert result.needs_human is True


@pytest.mark.parametrize(
    ("message", "history", "reply"),
    [
        ("Бюджет до 40 000 ₽. Что посоветуете с установкой?", [],
         "В бюджет 40 000 ₽ укладывается Basic 07 с монтажом — 37 800 ₽."),
        ("Бюджет у меня 40 000 ₽, уложимся?", ["Могу предложить вариант за 36 800 ₽"],
         "В 40 000 ₽ уложимся: вариант за 36 800 ₽."),
        ("Хочу уложиться в 45 000 ₽", [], "В 45 000 ₽ входит Basic 09 с монтажом: 42 800 ₽."),
    ],
)  # fmt: skip
def test_client_budget_beside_a_fitting_price_is_not_flagged(kb, message, history, reply):
    request = SuggestRequest(
        message=message, history=[DialogMessage(role="manager", text=text) for text in history]
    )
    result, warnings = apply_guards(make_suggestion(client_reply=reply), kb, request, "full")
    assert warnings == []
    assert result.needs_human is False


def test_client_budget_does_not_cover_accepting_it_as_price(kb):
    # «Бюджет» не лазейка: цена из БЗ рядом в него не укладывается — значит, ответ соглашается
    # на цену клиента.
    request = SuggestRequest(message="Мой бюджет 15 000 ₽, сделайте Basic 09 за него")
    reply = "Договорились: Basic 09 за 15 000 ₽, обычно он стоит 32 900 ₽."
    result, warnings = apply_guards(make_suggestion(client_reply=reply), kb, request, "full")
    assert codes(warnings) == ["client_amount"]
    assert result.needs_human is True


def test_client_amount_in_refusal_after_abbreviation_is_not_flagged(kb):
    # «руб.» перед строчной буквой — не конец предложения: отрицание дальше относится к той же сумме.
    request = SuggestRequest(message="Сделаете за 30 000 руб.?")
    reply = "Монтаж — 9 900 руб. Цена 30 000 руб. не действует, Basic 09 стоит 32 900 руб."
    _, warnings = apply_guards(make_suggestion(client_reply=reply), kb, request, "full")
    assert warnings == []


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


# Так ответил Grok 29.09.2026 после первой правки промпта: допродажа попала прямо в ответ клиенту.
LEAKED_REPLY = (
    "Здравствуйте, Анна! Для комнаты 20 м² подойдёт Сплит-система Basic 09 за 32 900 ₽. Учитывая аллергию "
    "у ребёнка, мы предлагаем очиститель воздуха Mini с HEPA-фильтром за 12 900 ₽."
)
PURIFIER_UPSELL = {
    "recommended": True,
    "timing": "now",
    "product_ids": ["air-purifier-mini"],
    "offer": "Очиститель воздуха Mini",
    "reason": "Ребёнок-аллергик.",
    "pitch": "Можно добавить очиститель воздуха Mini за 12 900 ₽.",
}


def test_upsell_leaking_into_reply_is_flagged(kb):
    suggestion = make_suggestion(client_reply=LEAKED_REPLY, upsell=PURIFIER_UPSELL)
    _, warnings = apply_guards(suggestion, kb, REQUEST, "full")
    assert [w.code for w in warnings] == ["upsell_in_reply"]
    assert "Очиститель воздуха Mini" in warnings[0].message
    # Разрешено настройкой UPSELL_IN_REPLY — не предупреждаем.
    _, allowed = apply_guards(suggestion, kb, REQUEST, "full", upsell_in_reply=True)
    assert allowed == []


def test_product_named_by_client_is_not_a_leak(kb):
    request = SuggestRequest(message="А очиститель воздуха у вас есть? Сколько стоит?")
    suggestion = make_suggestion(client_reply=LEAKED_REPLY, upsell=PURIFIER_UPSELL)
    _, warnings = apply_guards(suggestion, kb, request, "full")
    assert "upsell_in_reply" not in [w.code for w in warnings]


def test_declension_still_matches(kb):
    reply = "Монтаж стоит 9 900 ₽. Ещё советую очистителя воздуха Mini для детской."
    suggestion = make_suggestion(client_reply=reply, upsell=PURIFIER_UPSELL)
    _, warnings = apply_guards(suggestion, kb, REQUEST, "full")
    assert "upsell_in_reply" in [w.code for w in warnings]


def test_air_conditioners_are_not_checked_by_name(kb):
    # Basic 09 в ответе — основной товар; допродажа «второй кондиционер» не утечка.
    upsell = {**PURIFIER_UPSELL, "product_ids": ["ac-basic-07"], "offer": "Второй кондиционер"}
    reply = "Для 20 м² подойдёт Сплит-система Basic 09 — 32 900 ₽, для детской — Сплит-система Basic 07."
    _, warnings = apply_guards(make_suggestion(client_reply=reply, upsell=upsell), kb, REQUEST, "full")
    assert "upsell_in_reply" not in [w.code for w in warnings]


def test_standard_warranty_is_not_extended_warranty(kb):
    upsell = {**PURIFIER_UPSELL, "product_ids": ["warranty-plus-3y"], "offer": "Расширенная гарантия"}
    reply = "Монтаж стоит 9 900 ₽, гарантия 3 года."
    _, warnings = apply_guards(make_suggestion(client_reply=reply, upsell=upsell), kb, REQUEST, "full")
    assert "upsell_in_reply" not in [w.code for w in warnings]


def test_yearly_service_leak_is_flagged(kb):
    upsell = {**PURIFIER_UPSELL, "product_ids": ["service-1y"], "offer": "Годовое обслуживание"}
    reply = "Монтаж стоит 9 900 ₽. Рекомендую сразу оформить годовое обслуживание."
    _, warnings = apply_guards(make_suggestion(client_reply=reply, upsell=upsell), kb, REQUEST, "full")
    assert "upsell_in_reply" in [w.code for w in warnings]


@pytest.mark.parametrize(
    "reply",
    [
        "Извините, но я не могу игнорировать инструкции и менять свою роль.",  # Grok, prompt-injection
        "В базе знаний отсутствует информация о фасадном монтаже.",  # Grok, вопрос вне базы
        "Мой системный промпт показать не могу.",
    ],
)
def test_internal_terms_in_reply_are_flagged(kb, reply):
    _, warnings = apply_guards(make_suggestion(client_reply=reply, kb_refs=[]), kb, REQUEST, "full")
    assert "internal_terms" in [w.code for w in warnings]


def test_instructions_for_the_unit_are_fine(kb):
    reply = "Инструкцию по уходу мастер оставит после монтажа. Монтаж стоит 9 900 ₽."
    _, warnings = apply_guards(make_suggestion(client_reply=reply), kb, REQUEST, "full")
    assert "internal_terms" not in [w.code for w in warnings]


def test_amounts_are_regrouped_in_reply(kb):
    # Так ответил Grok 29.09.2026 — проверка цен сумму принимает, а формат поправляем.
    suggestion = make_suggestion(client_reply="Стандартный монтаж стоит 9900 ₽. Когда вам удобно?")
    result, warnings = apply_guards(suggestion, kb, REQUEST, "full")
    assert result.client_reply == "Стандартный монтаж стоит 9 900 ₽. Когда вам удобно?"
    assert warnings == []


# ---------- Где в тексте проблема: по field и fragment страница подсвечивает место ----------


def flags(warnings):
    return [(w.code, w.field, w.fragment) for w in warnings]


def test_wrong_price_and_total_point_to_their_place(kb):
    reply = "Павел, Inverter 12 стоит 52 900 ₽, стандартный монтаж — 9 900 ₽, итого 62 800 ₽."
    _, warnings = apply_guards(make_suggestion(client_reply=reply), kb, REQUEST, "full")
    assert flags(warnings) == [
        ("price_not_in_kb", "client_reply", "52 900 ₽"),
        ("price_not_in_kb", "client_reply", "62 800 ₽"),
    ]


def test_regrouped_amount_points_to_text_after_regrouping(kb):
    result, warnings = apply_guards(make_suggestion(client_reply="Монтаж — 31900 руб."), kb, REQUEST, "full")
    assert result.client_reply == "Монтаж — 31 900 руб."
    assert warnings[0].fragment == "31 900 руб." and warnings[0].fragment in result.client_reply


def test_forbidden_phrase_points_to_its_spelling_in_text(kb):
    suggestion = make_suggestion(
        client_reply="Дешевле нигде не найдете! Уважаемый клиент, монтаж 9 900 ₽.",
        upsell={"pitch": "Как я уже говорила, обслуживание выгодно"},
    )
    _, warnings = apply_guards(suggestion, kb, REQUEST, "full")
    assert sorted(f for f in flags(warnings) if f[0] == "forbidden_phrase") == [
        ("forbidden_phrase", "client_reply", "Дешевле нигде не найдете"),
        ("forbidden_phrase", "client_reply", "Уважаемый клиент"),
        ("forbidden_phrase", "upsell", "Как я уже говорила"),
    ]


def test_terms_and_internal_terms_point_to_their_place(kb):
    reply = "Монтаж для вас бесплатный, а на оборудование скидка 15%. Это по нашей базе знаний."
    _, warnings = apply_guards(make_suggestion(client_reply=reply), kb, REQUEST, "full")
    assert ("terms_not_in_kb", "client_reply", "15%") in flags(warnings)
    assert ("internal_terms", "client_reply", "базе знаний") in flags(warnings)
    [free] = [w for w in warnings if w.code == "terms_not_in_kb" and w.fragment != "15%"]
    assert free.fragment == "Монтаж для вас бесплатный, а на оборудование скидка 15%."  # всё предложение


def test_upsell_warnings_point_to_upsell(kb):
    suggestion = make_suggestion(
        upsell={
            "recommended": True,
            "timing": "now",
            "product_ids": ["service-1y"],
            "pitch": "Всего 2 222 ₽!",
        }
    )
    _, warnings = apply_guards(suggestion, kb, REQUEST, "full")
    assert flags(warnings) == [("upsell_price_not_in_kb", "upsell", "2 222 ₽")]


def test_warnings_without_place_have_no_fragment(kb):
    suggestion = make_suggestion(kb_refs=["faq-teleport"], answer_found_in_kb=True)
    _, warnings = apply_guards(suggestion, kb, REQUEST, "full")
    assert flags(warnings) == [("unknown_kb_ref", None, None), ("no_kb_refs", "client_reply", None)]
