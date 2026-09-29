import pytest

from app.core.money import extract_amounts, format_rub
from app.core.pii import mask_pii


def test_format_rub():
    assert format_rub(32900) == "32 900 ₽"
    assert format_rub(990) == "990 ₽"
    assert format_rub(1234567) == "1 234 567 ₽"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Basic 09 — 32 900 ₽, монтаж 9900 руб.", [32900, 9900]),
        ("итого 42 800 ₽", [42800]),
        ("доставка 990р. и 1 500 рублей", [990, 1500]),
        ("скидка 1 180,50 ₽", [1180.5]),
        ("комната 20 м², выезд через 1–3 дня, модель 09", []),
        ("5% на оборудование и 20% на обслуживание", []),
        ("2 × 3 900 ₽ = 7 800 ₽", [3900, 7800]),
        ("рублевый счёт", []),
    ],
)
def test_extract_amounts(text, expected):
    assert [value for _, value in extract_amounts(text)] == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Мой номер +7 (916) 123-45-67, звоните", "Мой номер [PHONE], звоните"),
        ("8 916 123 45 67", "[PHONE]"),
        ("89161234567", "[PHONE]"),
        ("пишите на anna.k+crm@mail.ru", "пишите на [EMAIL]"),
        ("карта 4111 1111 1111 1111", "карта [CARD]"),
        ("+375 29 123 45 67", "[PHONE]"),
    ],
)
def test_mask_pii(text, expected):
    assert mask_pii(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Basic 09 — 32 900 ₽, итого 42 800 ₽",
        "номер заказа 1234 5678 9012 3456",  # не проходит проверку Луна — не карта
        "8 900 ₽ за монтаж",
        "комната 20 м², 14 этаж",
    ],
)
def test_mask_pii_keeps_non_personal_data(text):
    assert mask_pii(text) == text
