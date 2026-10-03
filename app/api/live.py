"""Живая модель за паролем на публичном стенде (LLM_MODE=mock + LIVE_DEMO_PASSWORD).

Без пароля страница отвечает записанными ответами — посетители ничего не тратят. Кто ввёл пароль,
получает ответы живых моделей: пароль приходит в заголовке X-Live-Password с каждым запросом
подсказки и сверяется на сервере. Защита от подбора и дневной лимит живых запросов считаются в памяти
экземпляра: на Vercel экземпляров может быть несколько, поэтому это страховка, а не точный учёт.
Точный предел расходов — квота ключа в самом шлюзе.
"""

import secrets
import time
from collections import deque
from datetime import date
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.core.assistant import Assistant

HEADER = "X-Live-Password"
MAX_FAILURES = 5  # неверных паролей с одного адреса за окно
MAX_FAILURES_TOTAL = 50  # со всех адресов за окно: подбор с множества адресов тоже упирается в предел
FAILURE_WINDOW_SECONDS = 600

router = APIRouter(prefix="/api/v1/live", tags=["live"])


def _expire(attempts: deque[float], now: float) -> None:
    while attempts and now - attempts[0] > FAILURE_WINDOW_SECONDS:
        attempts.popleft()


class LiveDemo:
    def __init__(self, password: str, assistant: Assistant, daily_limit: int):
        self._password = password.encode()
        self.assistant = assistant
        self._daily_limit = daily_limit
        self._failures: dict[str, deque[float]] = {}
        self._all_failures: deque[float] = deque()
        self._day = date.today()
        self._used = 0

    def _blocked(self, client: str) -> str | None:
        """Почему вход сейчас закрыт; None — открыт."""
        now = time.monotonic()
        _expire(self._all_failures, now)
        if len(self._all_failures) >= MAX_FAILURES_TOTAL:
            return "Слишком много неверных паролей — вход в живую модель закрыт на 10 минут"
        attempts = self._failures.get(client)
        if attempts is not None:
            _expire(attempts, now)
            if len(attempts) >= MAX_FAILURES:
                return "Слишком много неверных паролей — попробуйте через 10 минут"
        return None

    def _remember_failure(self, client: str) -> None:
        now = time.monotonic()
        self._all_failures.append(now)
        self._failures.setdefault(client, deque()).append(now)
        if len(self._failures) > MAX_FAILURES_TOTAL:
            # Адреса без свежих ошибок больше не нужны: память не растёт от перебора адресов.
            for address in [
                a for a, attempts in self._failures.items() if now - attempts[-1] > FAILURE_WINDOW_SECONDS
            ]:
                del self._failures[address]

    def check(self, password: str, client: str) -> None:
        """401 — неверный пароль, 429 — слишком много неверных попыток с этого адреса или со всех сразу."""
        blocked = self._blocked(client)
        if blocked:
            raise HTTPException(status_code=429, detail=blocked)
        if not secrets.compare_digest(password.encode(), self._password):
            self._remember_failure(client)
            raise HTTPException(status_code=401, detail="Неверный пароль живой модели")
        self._failures.pop(client, None)

    def spend(self) -> None:
        """429, если дневной лимит живых запросов этого экземпляра исчерпан."""
        today = date.today()
        if today != self._day:
            self._day, self._used = today, 0
        if self._used >= self._daily_limit:
            raise HTTPException(status_code=429, detail="Лимит живых запросов на сегодня исчерпан")
        self._used += 1


def client_address(request: Request) -> str:
    """Адрес клиента для защиты от подбора пароля.

    Без доверенного прокси — адрес соединения: X-Forwarded-For клиент пишет сам, и каждый новый адрес
    в нём обнулял бы блокировку. (Uvicorn сам подставляет адрес из X-Forwarded-For, только если соединение
    пришло от доверенного прокси — по умолчанию 127.0.0.1.) С TRUST_FORWARDED_FOR — правая запись
    X-Forwarded-For: её дописывает ближайший прокси, а Vercel заголовок целиком перезаписывает.
    """
    if request.app.state.settings.trust_forwarded_for:
        forwarded = [part.strip() for part in request.headers.get("x-forwarded-for", "").split(",")]
        if forwarded[-1]:
            return forwarded[-1]
    return request.client.host if request.client else "unknown"


def get_live_demo(request: Request) -> LiveDemo:
    live: LiveDemo | None = getattr(request.app.state, "live_demo", None)
    if live is None:
        raise HTTPException(status_code=404, detail="Живая модель на этом стенде не включена")
    return live


def resolve_assistant(request: Request, password: str | None) -> LiveDemo | None:
    """Живое демо, если пришёл верный пароль; None — запрос без пароля (обычный ассистент)."""
    if not password:
        return None
    live = get_live_demo(request)
    live.check(password, client_address(request))
    return live


class LoginBody(BaseModel):
    password: str = Field(min_length=1, max_length=200)


@router.post("/login", summary="Проверить пароль живой модели и получить список моделей")
async def login(body: LoginBody, request: Request) -> dict[str, Any]:
    live = get_live_demo(request)
    live.check(body.password, client_address(request))
    llms = live.assistant.llms
    return {"default": llms.default, "providers": llms.describe()}
