"""Просмотр поддельного amoCRM (только AMOCRM_MODE=mock): какие примечания «появились в сделках»."""

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request

from app.amocrm.fake import FakeAmoApi

router = APIRouter(prefix="/api/v1/amocrm-mock", tags=["amocrm-mock"])


def _fake(request: Request) -> FakeAmoApi:
    fake = getattr(request.app.state, "fake_amo", None)
    if fake is None:
        raise HTTPException(status_code=404, detail="Доступно только при AMOCRM_MODE=mock")
    return fake


@router.get("/notes", summary="Примечания, записанные в поддельный amoCRM")
async def notes(
    request: Request,
    entity_type: Literal["leads", "contacts"] | None = None,
    entity_id: int | None = None,
    after_id: int = 0,
) -> list[dict[str, Any]]:
    return _fake(request).list_notes(entity_type, entity_id, after_id)


@router.get("/leads", summary="Сделки поддельного аккаунта")
async def leads(request: Request) -> list[dict[str, Any]]:
    return _fake(request).list_leads()
