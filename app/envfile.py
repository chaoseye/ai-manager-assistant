"""Запись настроек в .env для консольных команд подключения (шлюз LLM, аккаунт amoCRM).

Секреты вводятся скрыто и не печатаются: в выводе только маска вида «sk-…5f46».
"""

import getpass
import shutil
import sys
from pathlib import Path

from app.config import BASE_DIR

ENV_PATH = BASE_DIR / ".env"
ENV_EXAMPLE_PATH = BASE_DIR / ".env.example"


def mask(secret: str) -> str:
    return f"{secret[:3]}…{secret[-4:]}" if len(secret) > 10 else "…"


def _quote(value: str) -> str:
    # python-dotenv: без кавычек значение обрезается на « #», а пробелы по краям теряются.
    if any(ch in value for ch in " #'\"\\") or value != value.strip():
        return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"
    return value


def ensure_env_file(path: Path) -> bool:
    """Создаёт .env из .env.example, если его ещё нет. True — файл только что создан."""
    if path.exists() or path != ENV_PATH or not ENV_EXAMPLE_PATH.exists():
        return False
    shutil.copyfile(ENV_EXAMPLE_PATH, path)
    return True


def update_env(path: Path, values: dict[str, str], header: str = "") -> None:
    """Заменяет строки KEY=… в .env (первую незакомментированную) или дописывает новые в конец
    под комментарием header."""
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
        if header:
            lines.append(f"# {header}")
        lines += [f"{key}={_quote(value)}" for key, value in pending.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def read_secret(label: str, existing: str | None) -> str | None:
    """Секрет из stdin (если он перенаправлен) или скрытым вводом. Пустой ввод — оставить сохранённый."""
    if not sys.stdin.isatty():
        value = sys.stdin.readline().strip()
    else:
        hint = " (Enter — оставить сохранённый)" if existing else ""
        value = getpass.getpass(f"{label}{hint}, ввод скрыт: ").strip()
    return value or existing
