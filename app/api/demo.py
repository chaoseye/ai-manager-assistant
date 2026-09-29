"""Демо-страница «диалоговое окно» и её готовые сценарии."""

import json
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from app.api.deps import get_settings_dep
from app.config import Settings
from app.core.schemas import DialogMessage, LeadContext

TEMPLATES = Jinja2Templates(directory=Path(__file__).resolve().parent.parent / "web" / "templates")

router = APIRouter(tags=["demo"])


class Scenario(BaseModel):
    id: str
    title: str
    description: str = ""
    channel: str | None = None
    lead: LeadContext = Field(default_factory=LeadContext)
    dialog: list[DialogMessage] = Field(min_length=1)


def load_scenarios(directory: Path) -> list[Scenario]:
    if not directory.is_dir():
        return []
    scenarios = [
        Scenario.model_validate(json.loads(path.read_text(encoding="utf-8")))
        for path in sorted(directory.glob("*.json"))
    ]
    return scenarios


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
