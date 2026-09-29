"""Зависимости роутов: доступ к объектам приложения и проверка токенов."""

import secrets

from fastapi import Header, HTTPException, Request

from app.config import Settings
from app.core.assistant import Assistant
from app.kb.loader import KnowledgeStore
from app.storage.repo import SuggestionRepo


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings


def get_assistant(request: Request) -> Assistant:
    return request.app.state.assistant


def get_kb_store(request: Request) -> KnowledgeStore:
    return request.app.state.kb_store


def get_repo(request: Request) -> SuggestionRepo:
    return request.app.state.repo


def _check_bearer(expected: str | None, authorization: str | None, name: str) -> None:
    if not expected:
        return  # токен не задан — доступ открыт (локальная разработка)
    provided = ""
    if authorization and authorization.lower().startswith("bearer "):
        provided = authorization[7:].strip()
    if not secrets.compare_digest(provided.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail=f"Нужен заголовок Authorization: Bearer <{name}>")


def require_api_token(request: Request, authorization: str | None = Header(default=None)) -> None:
    _check_bearer(request.app.state.settings.api_token, authorization, "API_TOKEN")


def require_admin_token(request: Request, authorization: str | None = Header(default=None)) -> None:
    _check_bearer(request.app.state.settings.admin_token, authorization, "ADMIN_TOKEN")
