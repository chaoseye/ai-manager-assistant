"""GET /api/v1/llm/providers — модели, между которыми можно переключаться."""

from typing import Any

from fastapi import APIRouter, Depends

from app.api.deps import get_assistant, require_api_token
from app.core.assistant import Assistant

router = APIRouter(prefix="/api/v1/llm", tags=["llm"], dependencies=[Depends(require_api_token)])


@router.get("/providers", summary="Модели для переключения и их состояние")
async def providers(assistant: Assistant = Depends(get_assistant)) -> dict[str, Any]:
    llms = assistant.llms
    return {
        "mode": llms.default_client.mode,
        "default": llms.default,
        "fallbacks": llms.fallbacks,
        "gateway_check": llms.gateway_check,
        "providers": llms.describe(),
    }
