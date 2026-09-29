"""Демо-страница «диалоговое окно» и её готовые сценарии."""

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.api.deps import get_settings_dep
from app.config import Settings
from app.scenarios import Scenario, load_scenarios

TEMPLATES = Jinja2Templates(directory=Path(__file__).resolve().parent.parent / "web" / "templates")

router = APIRouter(tags=["demo"])


@router.get("/api/v1/demo/scenarios", summary="Готовые сценарии для демо-страницы")
async def scenarios(settings: Settings = Depends(get_settings_dep)) -> list[Scenario]:
    return load_scenarios(settings.scenarios_dir)


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def index(request: Request) -> HTMLResponse:
    state = request.app.state
    return TEMPLATES.TemplateResponse(
        request,
        "index.html",
        {
            "llm_mode": state.assistant.llm.mode,
            "llm_model": state.assistant.llm.model,
            "kb_version": state.kb_store.current.version,
        },
    )
