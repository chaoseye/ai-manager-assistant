"""Маскирование персональных данных перед отправкой текста в LLM."""

import re

_SEP = r"[\s\- ]*"

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# Российские номера: +7 / 8, затем 10 цифр с любыми разделителями и скобками.
_PHONE_RU_RE = re.compile(
    rf"(?<![\d+])(?:\+7|8){_SEP}\(?{_SEP}\d{{3}}{_SEP}\)?{_SEP}\d{{3}}{_SEP}\d{{2}}{_SEP}\d{{2}}(?!\d)"
)
# Прочие международные номера: +код и 8–12 цифр.
_PHONE_INTL_RE = re.compile(r"(?<![\d\w])\+\d{1,3}(?:[\s\- ()]*\d){8,12}(?!\d)")
# Кандидаты в номера карт: 13–19 цифр подряд или группами; проверяются алгоритмом Луна.
_CARD_RE = re.compile(r"(?<!\d)\d(?:[ \-]?\d){12,18}(?!\d)")


def _luhn_ok(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        digit = int(char)
        if index % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def _mask_card(match: re.Match[str]) -> str:
    digits = re.sub(r"\D", "", match.group(0))
    return "[CARD]" if _luhn_ok(digits) else match.group(0)


def mask_pii(text: str) -> str:
    """Заменяет номера карт, e-mail и телефоны на [CARD], [EMAIL], [PHONE]."""
    text = _CARD_RE.sub(_mask_card, text)
    text = _EMAIL_RE.sub("[EMAIL]", text)
    text = _PHONE_RU_RE.sub("[PHONE]", text)
    text = _PHONE_INTL_RE.sub("[PHONE]", text)
    return text
