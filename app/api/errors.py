"""Единый формат ошибок: {"error": {"code": ..., "message": ..., "details"?: ...}}."""

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.llm import LLMError, LLMRefusedError, LLMUnavailableError
from app.kb.loader import KBValidationError

logger = logging.getLogger(__name__)

_HTTP_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    422: "invalid_request",
}


def error_response(status: int, code: str, message: str, details: Any = None) -> JSONResponse:
    body: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        body["details"] = jsonable_encoder(details)
    return JSONResponse(status_code=status, content={"error": body})


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        return error_response(422, "invalid_request", "Некорректный запрос", exc.errors())

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _HTTP_CODES.get(exc.status_code, "http_error")
        return error_response(exc.status_code, code, str(exc.detail))

    @app.exception_handler(KBValidationError)
    async def _kb(_: Request, exc: KBValidationError) -> JSONResponse:
        return error_response(422, "kb_invalid", "База знаний не прошла проверку", exc.errors)

    @app.exception_handler(LLMError)
    async def _llm(_: Request, exc: LLMError) -> JSONResponse:
        logger.warning("llm_error", extra={"fields": {"error_type": type(exc).__name__, "error": str(exc)}})
        if isinstance(exc, LLMUnavailableError):
            return error_response(503, "llm_unavailable", str(exc))
        if isinstance(exc, LLMRefusedError):
            return error_response(502, "llm_refused", str(exc))
        return error_response(502, "llm_bad_output", str(exc))

    @app.exception_handler(Exception)
    async def _internal(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("internal_error")
        return error_response(500, "internal", "Внутренняя ошибка сервиса")
