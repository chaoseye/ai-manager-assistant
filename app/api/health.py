"""GET /health — состояние сервиса."""

from typing import Any

from fastapi import APIRouter, Request

from app import __version__

router = APIRouter(tags=["health"])


@router.get("/health", summary="Состояние сервиса")
async def health(request: Request) -> dict[str, Any]:
    state = request.app.state
    llm = state.assistant.llm
    llm_problem = getattr(llm, "problem", None)
    body: dict[str, Any] = {
        "status": "degraded" if llm_problem else "ok",
        "version": __version__,
        "kb_version": state.kb_store.current.version,
        "llm_mode": llm.mode,
        "llm_model": llm.model,
        "amocrm": state.settings.amocrm_mode,
    }
    if llm_problem:
        body["llm_problem"] = llm_problem
    return body
