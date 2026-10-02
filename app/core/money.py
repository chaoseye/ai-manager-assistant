"""Денежные суммы в тексте: форматирование и извлечение."""

import re

# Разделитель разрядов: обычный, неразрывный и узкий неразрывный пробелы.
_SEP = "[   ]"
_CURRENCY = r"(?:₽|руб(?:лей|ля|ль|\.)?|р\.)(?![а-яёa-z0-9_])"
_THOUSANDS = r"тыс(?:яч[аи]?|\.)?"
# Целая часть: «32 900», «52.900» и «52,900» (точка или запятая перед ровно тремя цифрами — разряды), «9900».
# Число групп ограничено: без границы поиск по «111 111 111 …» без валюты идёт за O(n²).
_INTEGER = (
    rf"\d{{1,3}}(?:{_SEP}\d{{3}}){{1,6}}|\d{{1,3}}(?:\.\d{{3}}){{1,6}}|\d{{1,3}}(?:,\d{{3}}){{1,6}}|\d+"
)

# Сумма с явным признаком валюты: «32 900 ₽», «9900 руб.», «1 500 рублей», «990р.», «12 тыс. руб.».
# Числа без валюты («20 м²», «1–3 дня») не считаются суммами.
AMOUNT_RE = re.compile(
    rf"(?<![\d.,])({_INTEGER})(?:[.,](\d{{1,2}}))?{_SEP}?(?:({_THOUSANDS}){_SEP}?)?{_CURRENCY}",
    re.IGNORECASE,
)
# Нижняя граница диапазона вплотную перед суммой: «25 000–30 000 ₽», «от 25 до 30 тыс. ₽».
_RANGE_START_RE = re.compile(
    rf"(?<![\d.,])({_INTEGER})(?:[.,](\d{{1,2}}))?{_SEP}?(?:[–—-]|до){_SEP}?$", re.IGNORECASE
)
_RANGE_WINDOW = 40  # сколько символов перед суммой смотреть в поисках нижней границы


def format_rub(value: int) -> str:
    """32900 -> «32 900 ₽»."""
    return f"{value:,}".replace(",", " ") + " ₽"


_UNGROUPED_RE = re.compile(rf"(?<![\d.,])(\d{{4,}})(?={_SEP}?(?:₽|руб|р\.))", re.IGNORECASE)
_POINT_GROUPED_RE = re.compile(
    rf"(?<![\d.,])(\d{{1,3}}(?:[.,]\d{{3}}){{1,6}})(?={_SEP}?(?:₽|руб|р\.))", re.IGNORECASE
)


def group_digits(text: str) -> str:
    """«9900 ₽» и «9.900 ₽» -> «9 900 ₽»: суммы перед валютой получают пробелы между разрядами, как требует
    тон. Числа без валюты не трогаем."""
    text = _POINT_GROUPED_RE.sub(lambda m: re.sub("[.,]", " ", m.group(1)), text)
    return _UNGROUPED_RE.sub(lambda m: f"{int(m.group(1)):,}".replace(",", " "), text)


def _value(integer: str, fraction: str | None, thousands: str | None) -> float:
    value = int(re.sub(r"[   .,]", "", integer)) + (int(fraction) / 10 ** len(fraction) if fraction else 0)
    return float(value * 1000 if thousands else value)


def extract_amounts(text: str) -> list[tuple[str, float]]:
    """Все суммы в рублях из текста: [(как написано, значение)].

    Нижняя граница диапазона («25 000–30 000 ₽») — отдельная сумма, если она того же порядка, что и верхняя:
    так «Basic 09 — 32 900 ₽» не превращается в диапазон от 9 ₽.
    """
    amounts: list[tuple[str, float]] = []
    for match in AMOUNT_RE.finditer(text):
        value = _value(match.group(1), match.group(2), match.group(3))
        start = match.start()
        lower = _RANGE_START_RE.search(text, max(0, start - _RANGE_WINDOW), start)
        if lower:
            low = _value(lower.group(1), lower.group(2), match.group(3))
            if value / 10 <= low < value:
                amounts.append((text[lower.start() : match.end()].strip(), low))
        amounts.append((match.group(0).strip(), value))
    return amounts
