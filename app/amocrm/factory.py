"""Сборка клиента amoCRM по настройкам: настоящий API или поддельный сервер (mock-режим)."""

import logging

import httpx

from app.amocrm.client import AmoClient, AmoError
from app.amocrm.fake import FakeAmoApi
from app.config import Settings

logger = logging.getLogger(__name__)

MOCK_TOKEN = "mock-token"


def build_amo_client(
    settings: Settings, transport: httpx.AsyncBaseTransport | None = None
) -> tuple[AmoClient, FakeAmoApi | None]:
    """В mock-режиме — тот же AmoClient, но поверх поддельного сервера с данными из AMOCRM_MOCK_SEED."""
    fake = None
    token = settings.amocrm_token or ""
    if settings.amocrm_mode == "mock" and transport is None:
        token = token or MOCK_TOKEN
        fake = FakeAmoApi.from_file(settings.amocrm_mock_seed, token=token)
        transport = fake.transport()
    client = AmoClient(
        settings.amocrm_api_base,
        token,
        rps=settings.amocrm_rps,
        timeout=settings.amocrm_timeout_seconds,
        transport=transport,
    )
    return client, fake


async def check_amocrm(amo: AmoClient) -> str | None:
    """None, если токен принят; иначе — понятное описание проблемы для /health."""
    try:
        account = await amo.get_account()
    except AmoError as exc:
        return f"amoCRM недоступен или отклонил токен: {exc}"
    logger.info("amocrm_connected", extra={"fields": {"account_id": account.get("id")}})
    return None
