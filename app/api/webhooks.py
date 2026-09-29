"""POST /webhooks/amocrm/{secret} — приём вебхуков amoCRM о сообщениях в чатах.

amoCRM ждёт ответ не больше 2 секунд и отключает хук после 100+ ошибок за 2 часа. Поэтому здесь только
разбор и запись в БД. Ошибки разбора — ответ 200 (повтор той же посылки не поможет). Внутренний сбой —
500, чтобы amoCRM повторил доставку позже.
"""

import logging
import secrets
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app.amocrm.webhooks import WebhookFormatError, parse_webhook

logger = logging.getLogger(__name__)

router = APIRouter(tags=["amocrm"])


@router.post("/webhooks/amocrm/{secret}", summary="Вебхук amoCRM: входящие и исходящие сообщения")
async def amocrm_webhook(secret: str, request: Request) -> dict[str, Any]:
    state = request.app.state
    settings = state.settings
    inbox = getattr(state, "inbox", None)
    if inbox is None:
        raise HTTPException(status_code=404, detail="Интеграция с amoCRM выключена (AMOCRM_MODE=off)")
    expected = settings.webhook_secret or ""
    if not expected or not secrets.compare_digest(secret.encode(), expected.encode()):
        raise HTTPException(status_code=404, detail="Не найдено")  # без секрета вебхук выключен

    body = await request.body()
    try:
        batch = parse_webhook(body, request.headers.get("content-type"))
    except WebhookFormatError as exc:
        logger.warning("webhook_bad_format", extra={"fields": {"error": str(exc), "size": len(body)}})
        return {"ok": True, "accepted": 0}

    if settings.amocrm_account_id and batch.account_id and batch.account_id != settings.amocrm_account_id:
        logger.warning(
            "webhook_foreign_account",
            extra={"fields": {"account_id": batch.account_id, "expected": settings.amocrm_account_id}},
        )
        return {"ok": True, "accepted": 0}
    if batch.skipped:
        logger.warning("webhook_items_skipped", extra={"fields": {"reasons": batch.skipped[:10]}})

    result = await inbox.ingest(batch)
    return {"ok": True, **result}
