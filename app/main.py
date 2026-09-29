"""Сборка FastAPI-приложения."""

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.amocrm.client import AmoClient
from app.amocrm.factory import build_amo_client, check_amocrm
from app.api import amocrm_mock, demo, health, kb, suggest, webhooks
from app.api.errors import register_error_handlers
from app.config import Settings, get_settings
from app.core.assistant import Assistant
from app.core.llm import LLMClient, build_llm_client
from app.kb.loader import KnowledgeStore
from app.logging_setup import configure_logging, request_id_var
from app.storage.db import Database
from app.storage.repo import DialogRepo, JobRepo, SuggestionRepo
from app.worker.processor import Inbox, Worker

STATIC_DIR = Path(__file__).resolve().parent / "web" / "static"


class ConfigError(RuntimeError):
    pass


def create_app(
    settings: Settings | None = None,
    *,
    llm: LLMClient | None = None,
    amo_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """settings, llm и транспорт amoCRM можно подменить в тестах."""
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(settings.log_level)
        errors = settings.amocrm_config_errors()
        if errors:
            raise ConfigError("Неполные настройки amoCRM:\n- " + "\n- ".join(errors))
        kb_store = KnowledgeStore(settings.kb_dir)  # ошибка в БЗ — сервис не стартует
        db = Database(settings.db_path)
        await db.connect()
        assistant = Assistant(kb_store, llm or build_llm_client(settings), settings)
        suggestions = SuggestionRepo(db)
        app.state.settings = settings
        app.state.kb_store = kb_store
        app.state.assistant = assistant
        app.state.repo = suggestions
        app.state.inbox = app.state.worker = app.state.jobs = app.state.dialogs = app.state.fake_amo = None
        app.state.amocrm_problem = None

        amo: AmoClient | None = None
        worker: Worker | None = None
        if settings.amocrm_mode != "off":
            amo, app.state.fake_amo = build_amo_client(settings, amo_transport)
            app.state.amocrm_problem = await check_amocrm(amo)
            dialogs, jobs = DialogRepo(db), JobRepo(db)
            app.state.jobs = jobs
            app.state.dialogs = dialogs
            app.state.inbox = Inbox(db, dialogs, jobs, settings)
            worker = Worker(
                settings=settings,
                assistant=assistant,
                amo=amo,
                db=db,
                dialogs=dialogs,
                jobs=jobs,
                suggestions=suggestions,
            )
            app.state.worker = worker
            if settings.worker_enabled:
                await worker.start()
        try:
            yield
        finally:
            if worker is not None:
                await worker.stop()
            if amo is not None:
                await amo.aclose()
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
    app.include_router(webhooks.router)
    app.include_router(amocrm_mock.router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


app = create_app()
