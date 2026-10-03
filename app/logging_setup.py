"""JSON-логи с request_id."""

import contextvars
import json
import logging
import re
import sys
from datetime import UTC, datetime

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)

# Секрет вебхука — часть пути (подписи у amoCRM нет). Журнал запросов uvicorn пишет путь целиком.
_WEBHOOK_SECRET_RE = re.compile(r"(/webhooks/amocrm/)[^/?#\s\"]+")


def mask_webhook_secret(text: str) -> str:
    return _WEBHOOK_SECRET_RE.sub(r"\1***", text)


class WebhookSecretFilter(logging.Filter):
    """Заменяет секрет в пути вебхука на *** во всех аргументах записи журнала."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = mask_webhook_secret(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(mask_webhook_secret(a) if isinstance(a, str) else a for a in record.args)
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        request_id = request_id_var.get()
        if request_id:
            payload["request_id"] = request_id
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            payload.update(fields)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for noisy in ("httpx", "httpx2", "httpcore", "httpcore2", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # Журнал запросов uvicorn настраивает сам и пишет мимо корневого обработчика — фильтр вешаем на логгер.
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, WebhookSecretFilter) for f in access.filters):
        access.addFilter(WebhookSecretFilter())
