"""Суммы и ПДн: свойства на случайных входах (hypothesis) и формы записи, которые встречаются в ответах."""

import re

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.core.money import extract_amounts, format_rub, group_digits
from app.core.pii import _luhn_ok, mask_pii

SEPS = [" ", " ", " "]
CURRENCIES = ["₽", "руб.", "руб", "рублей", "рубля", "р."]


# ---------- Свойства ----------


@given(st.integers(min_value=1, max_value=10**9))
def test_format_then_extract_roundtrip(value):
    assert extract_amounts(format_rub(value)) == [(format_rub(value), float(value))]


@given(
    st.integers(min_value=1000, max_value=10**8),
    st.sampled_from(CURRENCIES),
    st.sampled_from(["", " ", " "]),
)
def test_group_digits_keeps_value_and_is_idempotent(value, currency, gap):
    text = f"Итого {value}{gap}{currency}, спасибо."
    once = group_digits(text)
    assert group_digits(once) == once
    assert [v for _, v in extract_amounts(once)] == [float(value)]


@given(st.text(max_size=300))
def test_money_functions_never_crash(text):
    extract_amounts(text)
    assert group_digits(group_digits(text)) == group_digits(text)


@given(st.text(max_size=300))
def test_mask_pii_is_idempotent(text):
    once = mask_pii(text)
    assert mask_pii(once) == once


@given(st.integers(min_value=100, max_value=999_999), st.sampled_from(SEPS))
def test_prices_are_never_masked(value, sep):
    price = format_rub(value).replace(" ", sep)
    assert mask_pii(f"Цена {price}, монтаж 9 900 ₽.") == f"Цена {price}, монтаж 9 900 ₽."


def _luhn_complete(prefix: str) -> str:
    return next(prefix + last for last in "0123456789" if _luhn_ok(prefix + last))


@given(st.text(alphabet="0123456789", min_size=15, max_size=15), st.sampled_from(["", " ", "-"]))
def test_any_luhn_valid_card_is_masked(body, sep):
    card = _luhn_complete(body)
    grouped = sep.join(card[i : i + 4] for i in range(0, 16, 4))
    masked = mask_pii(f"Моя карта {grouped}, спишите.")
    assert "[CARD]" in masked
    assert not re.search(r"\d{4}", masked)


@pytest.mark.parametrize(
    "phone",
    [
        "+7 999 123-45-67",
        "+7(999)123-45-67",
        "+7 (999) 123 45 67",
        "8 (999) 123-45-67",
        "8-999-123-45-67",
        "89991234567",
        "+79991234567",
        "8 999 1234567",
        "+7 999 123 45 67",
        "+375 29 123-45-67",
        "+44 20 7946 0958",
    ],
)
def test_phone_formats_are_masked(phone):
    assert mask_pii(f"Звоните: {phone}.") == "Звоните: [PHONE]."


@pytest.mark.parametrize(
    "email", ["anna@example.com", "a.b+tag@mail.ru", "иван@почта.рф", "x_y-z@sub.domain.co.uk"]
)
def test_email_formats_are_masked(email):
    assert mask_pii(f"Почта {email}, жду") == "Почта [EMAIL], жду"


def test_not_masked_by_design():
    # По ТЗ маскируются только телефоны, e-mail и карты; паспорт и адрес уходят в модель как есть.
    text = "Паспорт 4510 123456, адрес: ул. Ленина, д. 5, кв. 12"
    assert mask_pii(text) == text


def test_order_number_starting_with_8_looks_like_phone():
    # Ложное срабатывание, с которым можно жить: 11 цифр с 8 в начале — это и есть формат телефона.
    assert mask_pii("Номер заказа 81234567890") == "Номер заказа [PHONE]"


# ---------- Формы записи сумм, которые проверка цен должна видеть ----------
# Всё, чего не находит extract_amounts, уходит клиенту без проверки — даже выдуманная цена.


def test_dot_and_comma_thousands_separators():
    assert [v for _, v in extract_amounts("Итого 52.900 ₽, а не 52,900 руб.")] == [52900.0, 52900.0]
    assert group_digits("Итого 52.900 ₽") == "Итого 52 900 ₽"
    assert [v for _, v in extract_amounts("Скидка 52.90 ₽")] == [52.9]  # две цифры — копейки


def test_thousands_abbreviation():
    text = "Монтаж обойдётся в 12 тыс. руб., а с демонтажем 15,5 тысяч рублей"
    assert [v for _, v in extract_amounts(text)] == [12000.0, 15500.0]


def test_range_lower_bound():
    assert [v for _, v in extract_amounts("Фасадный монтаж — 25 000–30 000 ₽")] == [25000.0, 30000.0]
    assert [v for _, v in extract_amounts("от 25 до 30 тыс. ₽")] == [25000.0, 30000.0]


def test_amount_without_currency_after_price_word():
    assert extract_amounts("Basic 09 стоит 31 900, монтаж отдельно") == [("31 900", 31900.0)]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Итого: 42 800, когда удобно?", [42800]),
        ("цена — 9900 за всё", [9900]),
        ("Сделаете за 30 000?", [30000]),
        ("Бюджет до 40 000, что посоветуете?", [40000]),
        ("обслуживание стоит 5 900 минимум в год", [5900]),
        # Без слова о цене, меньше 1000 или с единицей измерения — не сумма.
        ("Монтаж займёт 3–4 часа, приедем за 2 часа до начала", []),
        ("Гарантия на 3 года, оплата за 2026 год", []),
        ("доставка за пределы МКАД — до 1 500 км не возим", []),
        ("за 12 месяцев гарантии, за 1 500 минут, стоит 3 000 кв. м", []),
        ("Заказ №123456 оплачен", []),
        # С валютой — как раньше, без двойного счёта.
        ("За 25 000–30 000 ₽ сделаем фасадный монтаж", [25000, 30000]),
        ("Итого 42 800 ₽, стоит 31 900 без монтажа", [42800, 31900]),
    ],
)
def test_amounts_without_currency(text, expected):
    assert [round(v) for _, v in extract_amounts(text)] == expected


@given(st.integers(min_value=1000, max_value=10**8), st.sampled_from(CURRENCIES))
def test_price_word_with_currency_is_counted_once(value, currency):
    assert [v for _, v in extract_amounts(f"Итого {format_rub(value)[:-2]} {currency}")] == [float(value)]


def test_model_number_before_dash_is_not_an_amount():
    # «Basic 09 — 32 900 ₽»: «09» перед тире — номер модели, а не начало диапазона.
    assert [v for _, v in extract_amounts("Basic 09 — 32 900 ₽, Inverter 12 – 54 900 ₽")] == [
        32900.0,
        54900.0,
    ]
