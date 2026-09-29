"""Регистрирует вебхук сервиса в amoCRM: события «входящее» и «исходящее сообщение».

    python -m app.amocrm.setup_webhook https://<публичный-адрес-сервиса>

Нужны AMOCRM_MODE=live, AMOCRM_SUBDOMAIN, AMOCRM_TOKEN и WEBHOOK_SECRET в .env.
То же можно сделать вручную: Настройки → Интеграции → Web hooks.
"""

import argparse
import asyncio
import sys

import httpx

from app.amocrm.client import AmoError
from app.amocrm.factory import build_amo_client
from app.config import Settings, get_settings

EVENTS = ["add_message", "add_outgoing_message"]


async def register(
    public_url: str, settings: Settings, transport: httpx.AsyncBaseTransport | None = None
) -> str:
    if settings.amocrm_mode != "live":
        raise ValueError("Регистрация вебхука имеет смысл только с AMOCRM_MODE=live")
    errors = settings.amocrm_config_errors()
    if errors:
        raise ValueError("; ".join(errors))
    if not public_url.startswith("https://"):
        raise ValueError("Адрес должен начинаться с https:// — amoCRM шлёт вебхуки на публичный HTTPS")
    destination = f"{public_url.rstrip('/')}/webhooks/amocrm/{settings.webhook_secret}"
    amo, _ = build_amo_client(settings, transport)
    try:
        await amo.subscribe_webhook(destination, EVENTS)
    finally:
        await amo.aclose()
    return destination


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(
        prog="python -m app.amocrm.setup_webhook", description=__doc__.splitlines()[0]
    )
    parser.add_argument(
        "public_url", help="публичный HTTPS-адрес сервиса (например, из ngrok или cloudflared)"
    )
    args = parser.parse_args(argv)
    settings = get_settings()
    try:
        destination = asyncio.run(register(args.public_url, settings))
    except (ValueError, AmoError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 2
    masked = destination.replace(settings.webhook_secret or "", "***")
    print(f"Вебхук зарегистрирован: {masked} ({', '.join(EVENTS)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
