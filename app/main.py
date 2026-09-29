"""Сборка FastAPI-приложения."""

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.api import demo, health, kb, suggest
from app.api.errors import register_error_handlers
from app.config import Settings, get_settings
from app.core.assistant import Assistant
from app.core.llm import LLMClient, build_llm_client
from app.kb.loader import KnowledgeStore
from app.logging_setup import configure_logging, request_id_var
from app.storage.db import Database
from app.storage.repo import SuggestionRepo

STATIC_DIR = Path(__file__).resolve().parent / "web" / "static"


def create_app(settings: Settings | None = None, *, llm: LLMClient | None = None) -> FastAPI:
    """settings и llm можно подменить в тестах."""
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(settings.log_level)
        kb_store = KnowledgeStore(settings.kb_dir)  # ошибка в БЗ — сервис не стартует
        db = Database(settings.db_path)
        await db.connect()
        app.state.settings = settings
        app.state.kb_store = kb_store
        app.state.assistant = Assistant(kb_store, llm or build_llm_client(settings), settings)
        app.state.repo = SuggestionRepo(db)
        try:
            yield
        finally:
            await db.close()

    app = FastAPI(
        title="AI-помощник менеджера",
        description="Ответ клиенту по базе знаний и подсказка по допродаже для менеджера.",
        version=__version__,
        lifespan=lifespan,
    )
    register_error_handlers(app)

    @app.middleware("http")
    async def request_id_middleware(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        token = request_id_var.set(request_id)
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
        response.headers["X-Request-ID"] = request_id
        return response

    app.include_router(suggest.router)
    app.include_router(kb.router)
    app.include_router(health.router)
    app.include_router(demo.router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


app = create_app()
