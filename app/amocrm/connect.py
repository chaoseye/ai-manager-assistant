"""Подключение живого аккаунта amoCRM: адрес и долгосрочный токен — в .env, с проверкой токена.

    python -m app.amocrm.connect
    python -m app.amocrm.connect --account mycompany.amocrm.ru
    Get-Clipboard | python -m app.amocrm.connect --account mycompany      # токен из буфера (PowerShell)

Адрес аккаунта можно ввести как поддомен («mycompany»), адрес («mycompany.amocrm.ru») или ссылку из
браузера. Токен вводится скрыто и не печатается; Enter вместо токена оставляет сохранённый.
Перед сохранением команда проверяет токен запросом GET /api/v4/account — он ничего не меняет в аккаунте.
В .env записываются AMOCRM_MODE=live, AMOCRM_SUBDOMAIN (или AMOCRM_BASE_URL для kommo.com),
AMOCRM_TOKEN, AMOCRM_ACCOUNT_ID (вебхуки чужих аккаунтов игнорируются) и WEBHOOK_SECRET —
случайный, если сейчас там пусто или демо-значение dev-secret.
"""

import argparse
import asyncio
import re
import secrets
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.amocrm.client import AmoAuthError, AmoError, AmoRequestError
from app.amocrm.factory import build_amo_client
from app.config import Settings
from app.envfile import ENV_PATH, ensure_env_file, mask, read_secret, update_env

EXIT_OK = 0
EXIT_CHECK_FAILED = 1
EXIT_BAD_INPUT = 2
DEMO_SECRETS = {"dev-secret"}
_SUBDOMAIN_RE = re.compile(r"[a-z0-9][a-z0-9-]*")


def parse_account(value: str) -> tuple[str, str | None]:
    """(поддомен, адрес API или None) из «mycompany», «mycompany.amocrm.ru» или ссылки из браузера.
    Для аккаунтов не на amocrm.ru (kommo.com, amocrm.com) адрес API задаётся целиком."""
    raw = value.strip()
    if not raw or "<" in raw or ">" in raw:
        raise ValueError("укажите адрес аккаунта, например mycompany.amocrm.ru")
    host = urlsplit(raw if "://" in raw else f"https://{raw}").hostname or ""
    subdomain = host.split(".")[0]
    if not _SUBDOMAIN_RE.fullmatch(subdomain):
        raise ValueError(f"не похоже на адрес аккаунта amoCRM: «{value}»")
    if "." not in host or host.endswith(".amocrm.ru"):
        return subdomain, None
    return subdomain, f"https://{host}"


async def fetch_account(
    settings: Settings, transport: httpx.AsyncBaseTransport | None = None
) -> dict[str, Any]:
    """Данные аккаунта (id, name, …). RuntimeError с понятным текстом — если проверка не прошла."""
    amo, _ = build_amo_client(settings, transport)
    try:
        return await amo.get_account()
    except AmoAuthError as exc:
        raise RuntimeError(
            "amoCRM не принял токен: проверьте, что он скопирован целиком и не отозван, "
            f"а у интеграции есть доступ к данным аккаунта ({exc})"
        ) from exc
    except AmoRequestError as exc:
        raise RuntimeError(f"amoCRM отклонил запрос — проверьте адрес аккаунта ({exc})") from exc
    except AmoError as exc:
        raise RuntimeError(f"amoCRM недоступен: {exc}") from exc
    finally:
        await amo.aclose()


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.amocrm.connect",
        description="Сохранить адрес аккаунта amoCRM и долгосрочный токен в .env и проверить их.",
    )
    parser.add_argument("--account", help="поддомен или адрес аккаунта, например mycompany.amocrm.ru")
    parser.add_argument("--no-check", action="store_true", help="сохранить без проверки токена")
    parser.add_argument("--env-file", type=Path, default=ENV_PATH, help=argparse.SUPPRESS)
    return parser


def ask_account(current: Settings, given: str | None) -> str:
    if given:
        return given
    saved = current.amocrm_base_url or current.amocrm_subdomain or ""
    if not sys.stdin.isatty():
        if saved:
            return saved
        raise ValueError("укажите адрес аккаунта: --account mycompany.amocrm.ru")
    hint = f" [{saved}]" if saved else ""
    answer = input(f"Адрес аккаунта amoCRM, например mycompany.amocrm.ru{hint}: ").strip()
    return answer or saved


def main(argv: list[str] | None = None, *, transport: httpx.AsyncBaseTransport | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = make_parser().parse_args(argv)
    env_path: Path = args.env_file
    if ensure_env_file(env_path):
        print(f"Создан {env_path.name} из .env.example")
    current = Settings(_env_file=env_path if env_path.exists() else None)

    try:
        subdomain, base_url = parse_account(ask_account(current, args.account))
    except ValueError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return EXIT_BAD_INPUT
    token = read_secret("Долгосрочный токен amoCRM", current.amocrm_token)
    if not token:
        print("Ошибка: токен не введён", file=sys.stderr)
        return EXIT_BAD_INPUT

    secret = current.webhook_secret
    new_secret = not secret or secret in DEMO_SECRETS
    if new_secret:
        secret = secrets.token_urlsafe(24)
    settings = current.model_copy(
        update={
            "amocrm_mode": "live",
            "amocrm_subdomain": subdomain,
            "amocrm_base_url": base_url,
            "amocrm_token": token,
            "webhook_secret": secret,
        }
    )

    values = {
        "AMOCRM_MODE": "live",
        "AMOCRM_SUBDOMAIN": subdomain,
        "AMOCRM_BASE_URL": base_url or "",
        "AMOCRM_TOKEN": token,
        "WEBHOOK_SECRET": secret or "",
    }
    if not args.no_check:
        try:
            account = asyncio.run(fetch_account(settings, transport))
        except RuntimeError as exc:
            print(f"Проверка не прошла: {exc}. Ничего не сохранено.", file=sys.stderr)
            print("Сохранить без проверки: добавьте --no-check.", file=sys.stderr)
            return EXIT_CHECK_FAILED
        name = account.get("name") or "без названия"
        print(f"Токен принят: аккаунт «{name}», id {account.get('id')}, {settings.amocrm_api_base}.")
        if account.get("id"):
            values["AMOCRM_ACCOUNT_ID"] = str(account["id"])

    update_env(env_path, values, header="amoCRM (записано командой python -m app.amocrm.connect)")
    saved = ["AMOCRM_MODE=live", f"адрес {settings.amocrm_api_base}", f"AMOCRM_TOKEN={mask(token)}"]
    if "AMOCRM_ACCOUNT_ID" in values:
        saved.append(f"AMOCRM_ACCOUNT_ID={values['AMOCRM_ACCOUNT_ID']}")
    saved.append("WEBHOOK_SECRET — новый случайный" if new_secret else "WEBHOOK_SECRET — прежний")
    print(f"Сохранено в {env_path}: {', '.join(saved)}.")
    print("Дальше: перезапустите сервис, откройте его наружу по HTTPS и зарегистрируйте вебхук:")
    print("  cloudflared tunnel --url http://localhost:8000")
    print("  (если туннели Cloudflare заблокированы: ssh -p 443 -R0:localhost:8000 free.pinggy.io)")
    print("  python -m app.amocrm.setup_webhook https://<адрес-туннеля>")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
