"""Демо-страница «диалоговое окно» и её готовые сценарии."""

import hashlib
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.api.deps import get_settings_dep
from app.config import Settings
from app.scenarios import Scenario, load_scenarios

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
TEMPLATES = Jinja2Templates(directory=WEB_DIR / "templates")


def _static_version() -> str:
    """Хэш содержимого CSS и JS: адреса статики меняются с каждой их правкой, и после деплоя браузер
    не возьмёт из кэша старый скрипт к новой странице."""
    digest = hashlib.sha256()
    for path in sorted((WEB_DIR / "static").rglob("*")):
        if path.is_file():
            digest.update(path.name.encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()[:10]


STATIC_VERSION = _static_version()

router = APIRouter(tags=["demo"])


@router.get("/api/v1/demo/scenarios", summary="Готовые сценарии для демо-страницы")
async def scenarios(settings: Settings = Depends(get_settings_dep)) -> list[Scenario]:
    return load_scenarios(settings.scenarios_dir)


def _page_context(request: Request, active: str) -> dict[str, object]:
    state = request.app.state
    llms = state.assistant.llms
    return {
        "active": active,
        "llm_mode": llms.default_client.mode,
        "llm_model": llms.default_client.model,
        "llm_label": llms.label(llms.default),
        "providers": llms.describe(),
        "kb_version": state.kb_store.current.version,
        "amocrm_mode": state.settings.amocrm_mode,
        "debounce_seconds": state.settings.debounce_seconds,
        "static_version": STATIC_VERSION,
    }


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def index(request: Request) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(request, "index.html", _page_context(request, "direct"))


@router.get("/amocrm", response_class=HTMLResponse, include_in_schema=False)
async def amocrm_page(request: Request) -> HTMLResponse:
    """Карточка сделки поддельного amoCRM: чат и служебные примечания AI-помощника в одной ленте."""
    return TEMPLATES.TemplateResponse(request, "amocrm.html", _page_context(request, "amocrm"))
