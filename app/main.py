"""Сборка FastAPI-приложения."""

import logging
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
from app.api import amocrm_mock, demo, health, kb, live, suggest, webhooks
from app.api import llm as llm_api
from app.api.errors import register_error_handlers
from app.api.live import LiveDemo
from app.config import Settings, get_settings
from app.core.assistant import Assistant
from app.core.llm import LLMClient
from app.core.llm_registry import LLMRegistry, build_llms
from app.kb.loader import KnowledgeStore
from app.logging_setup import configure_logging, request_id_var
from app.storage.db import Database
from app.storage.repo import DialogRepo, JobRepo, SuggestionRepo
from app.worker.processor import Inbox, Worker

STATIC_DIR = Path(__file__).resolve().parent / "web" / "static"
logger = logging.getLogger(__name__)

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
}
# Страницы демо грузят только свои скрипты и стили, без встроенного кода: всё остальное запрещено, и
# вставленная в черновик разметка не выполнится, даже если где-то попадёт в HTML.
PAGE_CSP = (
    "default-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'self'; "
    "form-action 'self'; frame-ancestors 'none'"
)
PAGES = frozenset({"/", "/amocrm"})
# Остальной HTML (документация API берёт скрипты с CDN) — только запрет встраивания в чужие страницы.
FRAME_CSP = "frame-ancestors 'none'"


def _add_security_headers(request: Request, response: Response) -> None:
    for name, value in SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    if response.headers.get("content-type", "").startswith("text/html"):
        response.headers.setdefault("X-Frame-Options", "DENY")
        csp = PAGE_CSP if request.url.path in PAGES else FRAME_CSP
        response.headers.setdefault("Content-Security-Policy", csp)


class ConfigError(RuntimeError):
    pass


async def _live_demo(
    settings: Settings, kb_store: KnowledgeStore, live_llm: LLMRegistry | None
) -> LiveDemo | None:
    """Живая модель за паролем: только на стенде в mock-режиме и только если задан пароль."""
    if settings.llm_mode != "mock" or not settings.live_demo_password:
        return None
    live_settings = settings.model_copy(update={"llm_mode": "live"})
    llms = live_llm
    if llms is None:
        llms = build_llms(live_settings)
        await llms.check_gateway(live_settings)
    if all(item["available"] is False for item in llms.describe()):
        logger.warning("live_demo_disabled", extra={"fields": {"reason": "ни одна модель не настроена"}})
        return None
    assistant = Assistant(kb_store, llms, live_settings)
    return LiveDemo(settings.live_demo_password, assistant, settings.live_demo_daily_limit)


def create_app(
    settings: Settings | None = None,
    *,
    llm: LLMRegistry | LLMClient | None = None,
    live_llm: LLMRegistry | None = None,
    amo_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """settings, llm (реестр моделей или один клиент), live_llm (модели для живого демо за паролем)
    и транспорт amoCRM можно подменить в тестах."""
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
        llms = llm
        if llms is None:
            llms = build_llms(settings)
            # Какие модели доступны ключу шлюза: один бесплатный запрос, не дольше 10 с.
            await llms.check_gateway(settings)
        assistant = Assistant(kb_store, llms, settings)
        suggestions = SuggestionRepo(db)
        app.state.settings = settings
        app.state.kb_store = kb_store
        app.state.assistant = assistant
        app.state.repo = suggestions
        app.state.live_demo = await _live_demo(settings, kb_store, live_llm)
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
        _add_security_headers(request, response)
        return response

    app.include_router(suggest.router)
    app.include_router(llm_api.router)
    app.include_router(live.router)
    app.include_router(kb.router)
    app.include_router(health.router)
    app.include_router(demo.router)
    app.include_router(webhooks.router)
    app.include_router(amocrm_mock.router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


app = create_app()
