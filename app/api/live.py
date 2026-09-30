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
MAX_FAILURES = 5
FAILURE_WINDOW_SECONDS = 600

router = APIRouter(prefix="/api/v1/live", tags=["live"])


class LiveDemo:
    def __init__(self, password: str, assistant: Assistant, daily_limit: int):
        self._password = password.encode()
        self.assistant = assistant
        self._daily_limit = daily_limit
        self._failures: dict[str, deque[float]] = {}
        self._day = date.today()
        self._used = 0

    def _blocked(self, client: str) -> bool:
        attempts = self._failures.get(client)
        if not attempts:
            return False
        now = time.monotonic()
        while attempts and now - attempts[0] > FAILURE_WINDOW_SECONDS:
            attempts.popleft()
        return len(attempts) >= MAX_FAILURES

    def check(self, password: str, client: str) -> None:
        """401 — неверный пароль, 429 — слишком много неверных попыток с этого адреса."""
        if self._blocked(client):
            raise HTTPException(
                status_code=429, detail="Слишком много неверных паролей — попробуйте через 10 минут"
            )
        if not secrets.compare_digest(password.encode(), self._password):
            self._failures.setdefault(client, deque()).append(time.monotonic())
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
    # На Vercel адрес клиента ставит сама платформа в X-Forwarded-For.
    forwarded = request.headers.get("x-forwarded-for", "")
    return forwarded.split(",")[0].strip() or (request.client.host if request.client else "unknown")


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
