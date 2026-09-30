"""Регистрирует вебхук сервиса в amoCRM: события «входящее» и «исходящее сообщение».

    python -m app.amocrm.setup_webhook https://<публичный-адрес-сервиса>

Нужны AMOCRM_MODE=live, AMOCRM_SUBDOMAIN, AMOCRM_TOKEN и WEBHOOK_SECRET в .env.
Публичный адрес — любой HTTPS-туннель к сервису, например:
    cloudflared tunnel --url http://localhost:8000
    ssh -p 443 -R0:localhost:8000 free.pinggy.io    # если туннели Cloudflare блокирует провайдер

Перед регистрацией команда проверяет адрес: /health отвечает этот сервис с AMOCRM_MODE=live, а пустая
посылка на адрес вебхука принимается — значит, туннель жив и сервис запущен с тем же WEBHOOK_SECRET
(пропустить проверку: --no-check). amoCRM при регистрации сам ищет домен в DNS и отвечает «Invalid URL»,
если не успел его найти, — со свежим адресом туннеля так бывает, поэтому команда повторяет попытку.

То же можно сделать вручную: amoМаркет → «⋯» вверху справа → «Web-хуки»
(в старом интерфейсе amoCRM — «Настройки → Интеграции → Web hooks»).
"""

import argparse
import asyncio
import sys
from collections.abc import Callable, Sequence

import httpx

from app.amocrm.client import AmoError, AmoRequestError
from app.amocrm.factory import build_amo_client
from app.config import Settings, get_settings

EVENTS = ["add_message", "add_outgoing_message"]
# Паузы между попытками при «Invalid URL»: свежий адрес туннеля amoCRM находил в DNS и через 15 с,
# и только через минуту, поэтому паузы растут — всего около минуты.
INVALID_URL_RETRY_DELAYS = (5.0, 10.0, 20.0, 30.0)
CHECK_TIMEOUT_SECONDS = 15.0


def webhook_destination(public_url: str, settings: Settings) -> str:
    return f"{public_url.rstrip('/')}/webhooks/amocrm/{settings.webhook_secret}"


def validate(public_url: str, settings: Settings) -> None:
    """ValueError, если с такими настройками или адресом регистрировать вебхук бессмысленно."""
    if settings.amocrm_mode != "live":
        raise ValueError("Регистрация вебхука имеет смысл только с AMOCRM_MODE=live")
    errors = settings.amocrm_config_errors()
    if errors:
        raise ValueError("; ".join(errors))
    if not public_url.startswith("https://"):
        raise ValueError("Адрес должен начинаться с https:// — amoCRM шлёт вебхуки на публичный HTTPS")


def _invalid_url(exc: AmoRequestError) -> bool:
    return exc.status == 400 and "Invalid URL" in str(exc)


async def register(
    public_url: str,
    settings: Settings,
    transport: httpx.AsyncBaseTransport | None = None,
    *,
    retry_delays: Sequence[float] = INVALID_URL_RETRY_DELAYS,
    on_retry: Callable[[int, float], None] | None = None,
) -> str:
    """Подписывает адрес вебхука на события. При «Invalid URL» повторяет с паузами retry_delays;
    on_retry(номер следующей попытки, пауза) вызывается перед каждой паузой."""
    validate(public_url, settings)
    destination = webhook_destination(public_url, settings)
    attempts = len(retry_delays) + 1
    amo, _ = build_amo_client(settings, transport)
    try:
        for attempt in range(1, attempts + 1):
            try:
                await amo.subscribe_webhook(destination, EVENTS)
                return destination
            except AmoRequestError as exc:
                if not _invalid_url(exc):
                    raise
                if attempt == attempts:
                    raise AmoRequestError(
                        f"amoCRM не принял адрес {public_url.rstrip('/')} («Invalid URL») "
                        f"с {attempts} попыток. При регистрации amoCRM сам ищет домен в DNS и отклоняет "
                        "адрес, который не нашёл: проверьте, что адрес открывается в браузере, "
                        "и повторите через минуту",
                        exc.status,
                    ) from exc
            delay = retry_delays[attempt - 1]
            if on_retry is not None:
                on_retry(attempt + 1, delay)
            await asyncio.sleep(delay)
        raise AssertionError("недостижимо: последняя попытка либо вернула адрес, либо бросила ошибку")
    finally:
        await amo.aclose()


async def check_destination(
    public_url: str, settings: Settings, transport: httpx.AsyncBaseTransport | None = None
) -> str | None:
    """None — по адресу отвечает этот сервис с AMOCRM_MODE=live и тем же WEBHOOK_SECRET; иначе — что не так.
    Пустая посылка на вебхук ничего не записывает: в ней нет сообщений."""
    base = public_url.rstrip("/")
    async with httpx.AsyncClient(timeout=CHECK_TIMEOUT_SECONDS, transport=transport) as http:
        try:
            health = await http.get(f"{base}/health")
        except httpx.HTTPError as exc:
            return (
                f"{base}/health не открывается ({type(exc).__name__}): туннель не запущен или адрес устарел"
            )
        try:
            data = health.json()
        except ValueError:
            data = None
        if not isinstance(data, dict) or "amocrm" not in data:
            return (
                f"{base}/health ответил {health.status_code}, но это не ответ сервиса: "
                "туннель не подключён или ведёт не на тот порт"
            )
        if data["amocrm"] != "live":
            return (
                f"сервис за этим адресом запущен с AMOCRM_MODE={data['amocrm']}: примечания не попадут "
                "в настоящий amoCRM. Включите live (python -m app.amocrm.connect) и перезапустите сервис"
            )
        if data.get("amocrm_problem"):
            return f"сервис за этим адресом не может работать с amoCRM: {data['amocrm_problem']}"

        try:
            probe = await http.post(
                webhook_destination(base, settings),
                content=b"",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except httpx.HTTPError as exc:
            return f"адрес вебхука не открывается ({type(exc).__name__})"
    if probe.status_code == 404:
        return (
            "сервис за этим адресом не принял секрет вебхука — он запущен с другим WEBHOOK_SECRET. "
            "Перезапустите сервис, чтобы он прочитал .env"
        )
    if probe.status_code != 200:
        return f"пробная посылка на адрес вебхука получила ответ {probe.status_code}"
    return None


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(
        prog="python -m app.amocrm.setup_webhook", description=__doc__.splitlines()[0]
    )
    parser.add_argument(
        "public_url", help="публичный HTTPS-адрес сервиса (например, из cloudflared или pinggy)"
    )
    parser.add_argument("--no-check", action="store_true", help="не проверять адрес перед регистрацией")
    args = parser.parse_args(argv)
    settings = get_settings()

    retries: list[int] = []

    def on_retry(attempt: int, delay: float) -> None:
        retries.append(attempt)
        print(
            f"amoCRM ещё не видит домен («Invalid URL»), повтор через {delay:.0f} с "
            f"(попытка {attempt} из {len(INVALID_URL_RETRY_DELAYS) + 1})",
            file=sys.stderr,
            flush=True,
        )

    try:
        validate(args.public_url, settings)
        if not args.no_check:
            problem = asyncio.run(check_destination(args.public_url, settings))
            if problem:
                print(f"Проверка адреса не прошла: {problem}.", file=sys.stderr)
                print("Зарегистрировать без проверки: добавьте --no-check.", file=sys.stderr)
                return 1
            print("Адрес проверен: сервис отвечает, секрет вебхука совпадает.", flush=True)
        destination = asyncio.run(register(args.public_url, settings, on_retry=on_retry))
    except (ValueError, AmoError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 2
    masked = destination.replace(settings.webhook_secret or "", "***")
    print(f"Вебхук зарегистрирован: {masked} ({', '.join(EVENTS)})")
    if retries:
        # На живом аккаунте после такого первая доставка не дошла, а повтор amoCRM делает через 5 минут.
        print(
            "amoCRM не сразу нашёл домен — первый вебхук может прийти с опозданием до 5 минут. "
            "Отправьте пробное сообщение заранее, до показа.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
