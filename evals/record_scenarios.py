"""Записывает ответы настоящей модели на демо-сценарии в examples/mock_llm/ — их показывает mock-режим.

    python -m evals.record_scenarios --provider grok                # все сценарии, кроме ручных
    python -m evals.record_scenarios --provider kimi --only complaint,after-sale

Запрос к модели собирается так же, как на демо-странице: сообщения клиента подряд в конце диалога —
новое обращение, всё до них — история; данные сделки и канал — из сценария. Ответ сохраняется,
только если он проходит проверки ядра без предупреждений: иначе mock-демо показывало бы ошибку
модели как норму. Сценарий price-check не перезаписывается: в нём намеренно неверная цена,
чтобы показать работу проверки.

Нужен LLM_MODE=live. Каждый сценарий — один платный запрос к модели.
"""

import argparse
import asyncio
import json
import sys
from datetime import date
from pathlib import Path

from app.config import PROVIDER_IDS, Settings, get_settings
from app.core.assistant import Assistant
from app.core.llm import LLMError
from app.core.llm_registry import build_llms
from app.core.schemas import SuggestRequest
from app.kb.loader import KnowledgeStore
from app.logging_setup import configure_logging
from app.scenarios import Scenario, load_scenarios

HANDCRAFTED = {"price-check"}  # ответы, составленные вручную нарочно


def build_request(scenario: Scenario) -> SuggestRequest:
    """Как splitDialog() на демо-странице: хвост из сообщений клиента — обращение, остальное — история."""
    dialog = scenario.dialog
    start = len(dialog)
    while start > 0 and dialog[start - 1].role == "client":
        start -= 1
    if start == len(dialog):
        raise ValueError(f"сценарий {scenario.id} не заканчивается сообщением клиента")
    message = "\n".join(m.text for m in dialog[start:])
    return SuggestRequest(
        message=message, history=dialog[:start], lead=scenario.lead, channel=scenario.channel
    )


def recording_path(settings: Settings, scenario: Scenario) -> Path:
    """Файл с записью этого сценария (по id внутри), иначе — под именем файла сценария."""
    for path in sorted(settings.mock_llm_dir.glob("*.json")):
        if json.loads(path.read_text(encoding="utf-8")).get("id") == scenario.id:
            return path
    for path in sorted(settings.scenarios_dir.glob("*.json")):
        if json.loads(path.read_text(encoding="utf-8")).get("id") == scenario.id:
            return settings.mock_llm_dir / path.name
    return settings.mock_llm_dir / f"{scenario.id}.json"


async def record(args: argparse.Namespace) -> int:
    settings = get_settings()
    if settings.llm_mode != "live":
        print("Нужен LLM_MODE=live: записываются ответы настоящей модели", file=sys.stderr)
        return 2
    scenarios = load_scenarios(settings.scenarios_dir)
    if args.only:
        wanted = {s.strip() for s in args.only.split(",")}
        unknown = wanted - {s.id for s in scenarios}
        if unknown:
            print(f"Нет таких сценариев: {sorted(unknown)}", file=sys.stderr)
            return 2
        scenarios = [s for s in scenarios if s.id in wanted]
    skipped = [s.id for s in scenarios if s.id in HANDCRAFTED]
    scenarios = [s for s in scenarios if s.id not in HANDCRAFTED]
    for scenario_id in skipped:
        print(f"· {scenario_id}: ответ составлен вручную нарочно — не перезаписываю")

    llms = build_llms(settings)
    await llms.check_gateway(settings)
    provider = args.provider or llms.default
    if llms.get(provider).problem:
        print(f"Модель {llms.label(provider)} недоступна: {llms.get(provider).problem}", file=sys.stderr)
        return 2
    assistant = Assistant(KnowledgeStore(settings.kb_dir), llms, settings)

    saved = 0
    for scenario in scenarios:
        request = build_request(scenario)
        try:
            result = await assistant.suggest(request, provider=provider)
        except LLMError as exc:
            print(f"✗ {scenario.id}: модель не ответила — {exc}")
            continue
        if result.meta.warnings:
            messages = "; ".join(w.message for w in result.meta.warnings)
            print(f"✗ {scenario.id}: ответ не прошёл проверки ({messages}) — оставляю прежнюю запись")
            continue
        payload = {
            "id": scenario.id,
            "match": request.message,
            "model": result.meta.model,
            "recorded_at": date.today().isoformat(),
            "suggestion": result.suggestion.model_dump(),
        }
        path = recording_path(settings, scenario)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        saved += 1
        print(f"✓ {scenario.id}: {result.meta.model}, {result.meta.latency_ms / 1000:.1f} с → {path.name}")
    print(f"Записано {saved} из {len(scenarios)}.")
    return 0 if saved == len(scenarios) else 1


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(
        prog="python -m evals.record_scenarios", description=__doc__.split("\n")[0]
    )
    parser.add_argument("--provider", choices=PROVIDER_IDS, help="модель; по умолчанию — LLM_PROVIDER")
    parser.add_argument("--only", help="id сценариев через запятую")
    configure_logging("WARNING")
    return asyncio.run(record(parser.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
