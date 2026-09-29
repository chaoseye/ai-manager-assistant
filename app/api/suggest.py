"""POST /api/v1/suggest — ядро по HTTP; GET /api/v1/suggestions/{id} — сохранённый результат."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import get_assistant, get_repo, require_api_token
from app.core.assistant import Assistant
from app.core.schemas import SuggestRequest, SuggestResult
from app.storage.repo import SuggestionRepo

router = APIRouter(prefix="/api/v1", tags=["suggest"], dependencies=[Depends(require_api_token)])


@router.post("/suggest", response_model=SuggestResult, summary="Обращение → ответ клиенту и подсказка")
async def suggest(
    request: SuggestRequest,
    assistant: Assistant = Depends(get_assistant),
    repo: SuggestionRepo = Depends(get_repo),
) -> SuggestResult:
    result = await assistant.suggest(request)
    await repo.save(result, assistant.prepare_request(request))
    return result


@router.get("/suggestions/{suggestion_id}", summary="Сохранённая подсказка по id")
async def get_suggestion(suggestion_id: str, repo: SuggestionRepo = Depends(get_repo)) -> dict[str, Any]:
    record = await repo.get(suggestion_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Подсказка не найдена")
    return record
