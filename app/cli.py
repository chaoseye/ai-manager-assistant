"""CLI: обращение клиента → ответ клиенту и подсказка по допродаже.

Пример:
    python -m app.cli "Сколько стоит установка?" --history examples/dialog.json --lead examples/lead.json
    python -m app.cli "Сколько стоит установка?" --provider kimi --verbose
"""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter, ValidationError

from app.config import PROVIDER_IDS, get_settings
from app.core.assistant import Assistant
from app.core.llm import LLMError
from app.core.llm_registry import build_llms
from app.core.schemas import DialogMessage, LeadContext, SuggestRequest, SuggestResult
from app.kb.loader import KBValidationError, KnowledgeStore
from app.logging_setup import configure_logging

EXIT_OK = 0
EXIT_BAD_INPUT = 2
EXIT_LLM = 3

TIMING_LABELS = {"now": "сейчас", "after_resolution": "после решения вопроса", "not_now": "не предлагать"}


class InputError(Exception):
    pass


def _read_json(path: Path, what: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise InputError(f"{what}: файл не найден: {path}") from exc
    except json.JSONDecodeError as exc:
        raise InputError(f"{what}: некорректный JSON в {path}: {exc}") from exc


def build_request(args: argparse.Namespace) -> SuggestRequest:
    text = sys.stdin.read() if args.stdin else args.text
    if not text or not text.strip():
        raise InputError("Нужен текст обращения: аргументом или через --stdin")
    history: list[DialogMessage] = []
    lead: LeadContext | None = None
    try:
        if args.history:
            history = TypeAdapter(list[DialogMessage]).validate_python(_read_json(args.history, "--history"))
        if args.lead:
            lead = LeadContext.model_validate(_read_json(args.lead, "--lead"))
        return SuggestRequest(message=text, history=history, lead=lead, channel=args.channel)
    except ValidationError as exc:
        raise InputError(f"Некорректные входные данные:\n{exc}") from exc


def format_text(result: SuggestResult, verbose: bool) -> str:
    s, meta = result.suggestion, result.meta
    lines: list[str] = []
    if meta.mode == "full":
        lines += ["=== Ответ клиенту ===", s.client_reply]
        if s.kb_refs:
            lines.append("Основано на: " + ", ".join(s.kb_refs))
        if s.needs_human:
            lines.append(f"⚠ Нужна проверка менеджера: {s.needs_human_reason or 'причина не указана'}")
        lines.append("")

    u = s.upsell
    lines.append(f"=== Допродажа (только для менеджера) · {TIMING_LABELS[u.timing]} ===")
    if u.recommended:
        lines.append(f"Что:       {u.offer}" + (f" [{', '.join(u.product_ids)}]" if u.product_ids else ""))
        lines.append(f"Почему:    {u.reason}")
        if u.pitch:
            lines.append(f"Фраза:     «{u.pitch}»")
    else:
        lines.append(f"Не предлагать: {u.reason}")
    if u.avoid:
        lines.append(f"Не делать: {u.avoid}")

    if meta.warnings:
        lines += ["", "Предупреждения:"]
        lines += [f"- {w.message}" for w in meta.warnings]
    if verbose:
        usage = meta.usage
        lines += [
            "",
            f"LLM: {meta.llm_mode} · {meta.provider or '—'} · модель {meta.model} · "
            f"попыток {meta.attempts} · {meta.latency_ms} мс",
            f"Токены: вход {usage.input_tokens}, из кэша {usage.cache_read_input_tokens}, "
            f"запись в кэш {usage.cache_creation_input_tokens}, выход {usage.output_tokens}",
            f"Версия БЗ: {meta.kb_version} · id подсказки: {meta.suggestion_id}",
        ]
    return "\n".join(lines)


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli",
        description="AI-помощник менеджера: ответ клиенту по базе знаний и подсказка по допродаже.",
    )
    parser.add_argument("text", nargs="?", help="новое обращение клиента")
    parser.add_argument("--stdin", action="store_true", help="прочитать обращение из stdin")
    parser.add_argument("--history", type=Path, help="JSON-массив прошлых сообщений {role, text, ts}")
    parser.add_argument("--lead", type=Path, help="JSON с данными сделки")
    parser.add_argument("--channel", help="канал: telegram, whatsapp, site…")
    parser.add_argument(
        "--provider",
        choices=PROVIDER_IDS,
        help="модель; по умолчанию — LLM_PROVIDER (и запасные из LLM_FALLBACK_PROVIDERS)",
    )
    parser.add_argument("--json", action="store_true", dest="as_json", help="вывод в JSON")
    parser.add_argument("--verbose", action="store_true", help="показать модель, токены, задержку")
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = make_parser().parse_args(argv)
    settings = get_settings()
    configure_logging("WARNING")
    logging.getLogger("app").setLevel(logging.WARNING)

    try:
        request = build_request(args)
        kb_store = KnowledgeStore(settings.kb_dir)
    except (InputError, KBValidationError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return EXIT_BAD_INPUT

    assistant = Assistant(kb_store, build_llms(settings), settings)
    try:
        result = asyncio.run(assistant.suggest(request, provider=args.provider))
    except LLMError as exc:
        print(f"Ошибка LLM: {exc}", file=sys.stderr)
        return EXIT_LLM

    if args.as_json:
        print(result.model_dump_json(indent=2))
    else:
        print(format_text(result, args.verbose))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
