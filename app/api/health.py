"""GET /health — состояние сервиса."""

from typing import Any

from fastapi import APIRouter, Request

from app import __version__

router = APIRouter(tags=["health"])


@router.get("/health", summary="Состояние сервиса")
async def health(request: Request) -> dict[str, Any]:
    state = request.app.state
    llm = state.assistant.llm
    problems: dict[str, str] = {}
    if getattr(llm, "problem", None):
        problems["llm_problem"] = llm.problem
    if getattr(state, "amocrm_problem", None):
        problems["amocrm_problem"] = state.amocrm_problem

    body: dict[str, Any] = {
        "status": "degraded" if problems else "ok",
        "version": __version__,
        "kb_version": state.kb_store.current.version,
        "llm_mode": llm.mode,
        "llm_model": llm.model,
        "amocrm": state.settings.amocrm_mode,
        **problems,
    }
    jobs = getattr(state, "jobs", None)
    if jobs is not None:
        worker = state.worker
        body["queue"] = await jobs.counts()
        if worker is not None and worker.running:
            body["worker"] = "running"
        else:
            body["worker"] = "on_request" if state.settings.worker_on_request else "stopped"
    return body
