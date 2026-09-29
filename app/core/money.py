"""Денежные суммы в тексте: форматирование и извлечение."""

import re

# Разделитель разрядов: обычный, неразрывный и узкий неразрывный пробелы.
_SEP = "[   ]"

# Сумма с явным признаком валюты: «32 900 ₽», «9900 руб.», «1 500 рублей», «990р.».
# Числа без валюты («20 м²», «1–3 дня») не считаются суммами.
AMOUNT_RE = re.compile(
    rf"(?<![\d.,])(\d{{1,3}}(?:{_SEP}\d{{3}})+|\d+)(?:[.,](\d{{1,2}}))?{_SEP}?"
    r"(?:₽|руб(?:лей|ля|ль|\.)?|р\.)(?![а-яёa-z0-9_])",
    re.IGNORECASE,
)


def format_rub(value: int) -> str:
    """32900 -> «32 900 ₽»."""
    return f"{value:,}".replace(",", " ") + " ₽"


def extract_amounts(text: str) -> list[tuple[str, float]]:
    """Все суммы в рублях из текста: [(как написано, значение)]."""
    amounts: list[tuple[str, float]] = []
    for match in AMOUNT_RE.finditer(text):
        integer = int(re.sub(_SEP, "", match.group(1)))
        fraction = match.group(2)
        value = integer + (int(fraction) / 10 ** len(fraction) if fraction else 0)
        amounts.append((match.group(0).strip(), float(value)))
    return amounts
