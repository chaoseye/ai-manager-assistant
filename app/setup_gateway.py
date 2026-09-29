"""Подключение шлюза (агрегатора) LLM: адрес и ключ — в .env, проверка ключа и моделей.

    python -m app.setup_gateway --url https://<шлюз>/v1
    python -m app.setup_gateway --url https://<шлюз>/v1 --live        # и сразу включить LLM_MODE=live
    Get-Clipboard | python -m app.setup_gateway --url https://<шлюз>/v1   # ключ из буфера (PowerShell)

Ключ вводится скрыто и не печатается. Перед сохранением команда проверяет его запросом GET /models:
токены моделей не тратятся. Заодно она показывает, какие модели помощника есть в шлюзе.
Если адрес шлюза сменился (например, перезапустился туннель), запустите команду с новым --url
и нажмите Enter вместо ключа: сохранённый ключ останется прежним.
"""

import argparse
import asyncio
import difflib
import getpass
import re
import shutil
import sys
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from app.config import BASE_DIR, PROVIDER_IDS, Settings
from app.core.gateway_llm import list_gateway_models
from app.core.providers import PROVIDERS

ENV_PATH = BASE_DIR / ".env"
ENV_EXAMPLE_PATH = BASE_DIR / ".env.example"
EXIT_OK = 0
EXIT_CHECK_FAILED = 1
EXIT_BAD_INPUT = 2
_HOST_RE = re.compile(r"[a-z0-9.:-]+")  # имя хоста в ASCII (IDNA) или IP-адрес


def mask(secret: str) -> str:
    return f"{secret[:3]}…{secret[-4:]}" if len(secret) > 10 else "…"


def _quote(value: str) -> str:
    # python-dotenv: без кавычек значение обрезается на « #», а пробелы по краям теряются.
    if any(ch in value for ch in " #'\"\\") or value != value.strip():
        return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"
    return value


def update_env(path: Path, values: dict[str, str]) -> None:
    """Заменяет строки KEY=… в .env (первую незакомментированную) или дописывает новые в конец."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    pending = dict(values)
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        if key in pending:
            lines[index] = f"{key}={_quote(pending.pop(key))}"
    if pending:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("# Шлюз LLM (записано командой python -m app.setup_gateway)")
        lines += [f"{key}={_quote(value)}" for key, value in pending.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def url_problem(url: str) -> str | None:
    """Почему адрес шлюза не годится; None — годится."""
    if "<" in url or ">" in url:
        return (
            "в адресе остался шаблон вроде <новый-адрес> — подставьте настоящий адрес, "
            "например https://abc-def.trycloudflare.com/v1"
        )
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return f"адрес «{url}» должен начинаться с https:// (или http://) и содержать имя хоста"
    try:
        host = parts.hostname.encode("idna").decode("ascii")
    except UnicodeError:
        host = ""
    if not _HOST_RE.fullmatch(host):
        return f"некорректный адрес «{url}»: в имени хоста недопустимые символы"
    return None


def read_key(existing: str | None) -> str | None:
    """Ключ из stdin (если он перенаправлен) или скрытым вводом. Пустой ввод — оставить сохранённый."""
    if not sys.stdin.isatty():
        key = sys.stdin.readline().strip()
    else:
        hint = " (Enter — оставить сохранённый)" if existing else ""
        key = getpass.getpass(f"Ключ шлюза{hint}, ввод скрыт: ").strip()
    return key or existing


def models_report(available: list[str], settings: Settings) -> tuple[list[str], int]:
    """Строки «модель помощника → есть ли она в шлюзе» и число ненайденных."""
    lines, missing = [], 0
    for provider in PROVIDER_IDS:
        model = settings.gateway_model(provider)
        name = PROVIDERS[provider].name
        if model in available:
            lines.append(f"  ✓ {name}: {model}")
            continue
        missing += 1
        # Подсказываем только модели того же семейства (grok-…, kimi/…): чужие id только запутают.
        family = [m for m in available if provider in m.lower()]
        close = difflib.get_close_matches(model, family, n=3, cutoff=0)
        hint = f" — есть похожие: {', '.join(close)}" if close else ""
        lines.append(
            f"  ✗ {name}: «{model}» нет в шлюзе{hint}. Задайте LLM_GATEWAY_MODEL_{provider.upper()} в .env"
        )
    return lines, missing


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.setup_gateway",
        description="Сохранить адрес и ключ шлюза LLM в .env и проверить их.",
    )
    parser.add_argument("--url", help="адрес OpenAI-совместимого API шлюза, например https://<шлюз>/v1")
    parser.add_argument("--live", action="store_true", help="включить LLM_MODE=live")
    parser.add_argument("--provider", choices=PROVIDER_IDS, help="модель по умолчанию (LLM_PROVIDER)")
    parser.add_argument("--no-check", action="store_true", help="сохранить без проверки ключа")
    parser.add_argument("--env-file", type=Path, default=ENV_PATH, help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None, *, transport: httpx.AsyncBaseTransport | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = make_parser().parse_args(argv)
    env_path: Path = args.env_file
    if not env_path.exists() and ENV_EXAMPLE_PATH.exists() and env_path == ENV_PATH:
        shutil.copyfile(ENV_EXAMPLE_PATH, env_path)
        print(f"Создан {env_path.name} из .env.example")

    current = Settings(_env_file=env_path if env_path.exists() else None)
    url = (args.url or current.llm_gateway_url or "").strip().rstrip("/")
    if not url:
        print("Ошибка: укажите адрес шлюза: --url https://адрес-шлюза/v1", file=sys.stderr)
        return EXIT_BAD_INPUT
    problem = url_problem(url)
    if problem:
        print(f"Ошибка: {problem}", file=sys.stderr)
        return EXIT_BAD_INPUT
    key = read_key(current.llm_gateway_key)
    if not key:
        print("Ошибка: ключ не введён", file=sys.stderr)
        return EXIT_BAD_INPUT

    missing = 0
    if not args.no_check:
        try:
            available = asyncio.run(list_gateway_models(url, key, transport=transport))
        except RuntimeError as exc:
            print(f"Проверка не прошла: {exc}. Ничего не сохранено.", file=sys.stderr)
            print("Сохранить без проверки: добавьте --no-check.", file=sys.stderr)
            return EXIT_CHECK_FAILED
        print(f"Ключ принят. Моделей в шлюзе: {len(available)}.")
        lines, missing = models_report(available, current)
        print("\n".join(lines))

    values = {"LLM_GATEWAY_URL": url, "LLM_GATEWAY_KEY": key}
    if args.live:
        values["LLM_MODE"] = "live"
    if args.provider:
        values["LLM_PROVIDER"] = args.provider
    update_env(env_path, values)
    saved = [f"LLM_GATEWAY_URL={url}", f"LLM_GATEWAY_KEY={mask(key)}"]
    saved += [f"{name}={value}" for name, value in values.items() if name in ("LLM_MODE", "LLM_PROVIDER")]
    print(f"Сохранено в {env_path}: {', '.join(saved)}")

    mode = "live" if args.live else current.llm_mode
    if mode != "live":
        print("Сейчас LLM_MODE=mock — модели не вызываются. Включить: --live или LLM_MODE=live в .env.")
    if missing:
        print("Модели с ✗ будут недоступны, пока не задан их id в шлюзе.")
    print("Перезапустите сервис, чтобы он прочитал новые настройки.")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
