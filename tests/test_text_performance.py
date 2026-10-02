"""Время обработки враждебных текстов: регулярные выражения не должны работать за O(n²).

Сообщение клиента — до 10 000 символов, в запросе до 71 текста (сообщение, история, replies). Маскирование
и проверки выполняются в event loop: пока они идут, сервис не отвечает никому, в том числе вебхукам amoCRM
(таймаут 2 с). Порог с запасом: линейная обработка 10 000 символов — миллисекунды, квадратичная — секунды.

"""

import time

import pytest

from app.core.guards import apply_guards
from app.core.money import extract_amounts
from app.core.pii import mask_pii
from app.core.schemas import SuggestRequest
from tests.conftest import make_suggestion

N = 10_000
LIMIT_SECONDS = 0.25

HOSTILE = {
    "длинное слово": "а" * N,
    "цифры": "1" * N,
    "base64": "QUJD" * (N // 4),
    "8 и пробелы": "8" + " " * (N - 2) + "?",
    "+7 и дефисы": "+7" + "-" * (N - 2),
    "a. подряд": "a." * (N // 2),
}


def timed(func, *args) -> float:
    started = time.perf_counter()
    func(*args)
    return time.perf_counter() - started


@pytest.mark.parametrize("name", list(HOSTILE))
def test_mask_pii_is_linear(name):
    assert timed(mask_pii, HOSTILE[name]) < LIMIT_SECONDS


def test_extract_amounts_is_linear():
    assert timed(extract_amounts, "111 " * (N // 4)) < LIMIT_SECONDS


def test_guards_on_hostile_conversation(kb):
    request = SuggestRequest(message="111 " * (N // 4))
    assert timed(apply_guards, make_suggestion(), kb, request, "full") < LIMIT_SECONDS


def test_normal_text_is_fast():
    sentence = "Здравствуйте! Сколько стоит монтаж Basic 09 за 32 900 ₽? Мой телефон +7 999 123-45-67. "
    text = (sentence * 120)[:N]
    assert timed(mask_pii, text) < LIMIT_SECONDS
    assert timed(extract_amounts, text) < LIMIT_SECONDS


def test_largest_valid_request_is_fast(client):
    long_word = "а" * 9_999
    payload = {
        "message": long_word,
        "history": [{"role": "client", "text": long_word} for _ in range(500)],
        "replies": [{"role": "manager", "text": long_word} for _ in range(50)],
    }
    started = time.perf_counter()
    assert client.post("/api/v1/suggest", json=payload).status_code == 200
    assert time.perf_counter() - started < 5.0
