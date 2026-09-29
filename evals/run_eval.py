"""Eval-прогон: кейсы из cases.yaml через ядро, проверки ожидаемых свойств, отчёт в Markdown.

Примеры:
    python -m evals.run_eval                          # все кейсы, режим LLM из .env
    python -m evals.run_eval --only kb-payment,foreign-en
    python -m evals.run_eval --tags complaint,injection
    python -m evals.run_eval --record                 # сохранить ответы модели для mock-режима

Прогон на реальной модели (LLM_MODE=live) стоит денег: оценка стоимости печатается в отчёте.
В mock-режиме прогон проверяет только сам скрипт — ответов модели для большинства кейсов там нет.
"""

import argparse
import asyncio
import json
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, ValidationError

from app.config import BASE_DIR, Settings, get_settings
from app.core.assistant import Assistant
from app.core.llm import LLMError, build_llm_client
from app.core.schemas import DialogMessage, LeadContext, Suggestion, SuggestRequest, SuggestResult, Usage
from app.kb.loader import KnowledgeStore
from app.logging_setup import configure_logging

CASES_PATH = BASE_DIR / "evals" / "cases.yaml"
REPORTS_DIR = BASE_DIR / "evals" / "reports"

# $ за 1 млн токенов. Сверяйте с актуальным прайсом Anthropic перед выводами о стоимости.
PRICES_PER_MTOK: dict[str, dict[str, float]] = {
    "claude-opus-5-5": {"input": 4.00, "output": 20.00, "cache_read": 0.20, "cache_write": 5.00},
    "claude-opus-5": {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write": 6.25},
    "claude-opus-4-8": {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write": 6.25},
    "claude-sonnet-5-5": {"input": 2.00, "output": 10.00, "cache_read": 0.20, "cache_write": 2.50},
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00, "cache_read": 0.10, "cache_write": 1.25},
}
PRICE_WARNING_CODES = {"price_not_in_kb", "upsell_price_not_in_kb"}


class Case(BaseModel):
    id: str
    tags: list[str] = Field(default_factory=list)
    description: str = ""
    message: str
    history: list[DialogMessage] = Field(default_factory=list)
    lead: LeadContext | None = None
    channel: str | None = None
    allow_warnings: list[str] = Field(default_factory=list)
    expect: dict[str, Any] = Field(default_factory=dict)


class CaseResult(BaseModel):
    case: Case
    result: SuggestResult | None = None
    error: str | None = None
    failures: list[str] = Field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.error is None and not self.failures


# ---------- Проверки ----------


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def _contains_any(text: str, needles: list[str]) -> bool:
    lowered = text.lower()
    return any(needle.lower() in lowered for needle in needles)


Check = Callable[[Suggestion, Any], str | None]

CHECKS: dict[str, Check] = {
    "intent": lambda s, v: None if s.intent in _as_list(v) else f"intent={s.intent}, ожидалось {v}",
    "sentiment": lambda s, v: (
        None if s.sentiment in _as_list(v) else f"sentiment={s.sentiment}, ожидалось {v}"
    ),
    "answer_found_in_kb": lambda s, v: (
        None if s.answer_found_in_kb is v else f"answer_found_in_kb={s.answer_found_in_kb}"
    ),
    "needs_human": lambda s, v: None if s.needs_human is v else f"needs_human={s.needs_human}",
    "upsell_recommended": lambda s, v: (
        None if s.upsell.recommended is v else f"upsell.recommended={s.upsell.recommended}"
    ),
    "upsell_timing": lambda s, v: (
        None if s.upsell.timing in _as_list(v) else f"upsell.timing={s.upsell.timing}, ожидалось {v}"
    ),
    "kb_refs_any": lambda s, v: (
        None if set(s.kb_refs) & set(v) else f"kb_refs={s.kb_refs}, нужен любой из {v}"
    ),
    "kb_refs_all": lambda s, v: None if set(v) <= set(s.kb_refs) else f"kb_refs={s.kb_refs}, нужны все {v}",
    "upsell_products_any": lambda s, v: (
        None if set(s.upsell.product_ids) & set(v) else f"upsell={s.upsell.product_ids}, нужен любой из {v}"
    ),
    "upsell_products_none": lambda s, v: (
        None
        if not set(s.upsell.product_ids) & set(v)
        else f"upsell={s.upsell.product_ids}, не должно быть {v}"
    ),
    "reply_contains_any": lambda s, v: (
        None if _contains_any(s.client_reply, v) else f"в ответе нет ни одного из {v}"
    ),
    "reply_not_contains": lambda s, v: (
        None
        if not _contains_any(s.client_reply, v)
        else f"в ответе есть запрещённое: {[n for n in v if n.lower() in s.client_reply.lower()]}"
    ),
}


def run_checks(expect: dict[str, Any], suggestion: Suggestion) -> list[str]:
    failures: list[str] = []
    for key, value in expect.items():
        if key == "any_of":
            variants = [run_checks(variant, suggestion) for variant in value]
            if all(variants):
                failures.append(
                    "не выполнен ни один вариант any_of: " + " | ".join("; ".join(v) for v in variants)
                )
            continue
        failure = CHECKS[key](suggestion, value)
        if failure:
            failures.append(failure)
    return failures


def _validate_expect(case: Case, expect: dict[str, Any]) -> None:
    for key, value in expect.items():
        if key == "any_of":
            if not isinstance(value, list) or not all(isinstance(v, dict) for v in value):
                raise ValueError(f"{case.id}: any_of должен быть списком наборов проверок")
            for variant in value:
                _validate_expect(case, variant)
        elif key not in CHECKS:
            raise ValueError(f"{case.id}: неизвестная проверка «{key}»")


def load_cases(path: Path) -> list[Case]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    cases = [Case.model_validate(item) for item in raw]
    ids = [c.id for c in cases]
    duplicates = {i for i in ids if ids.count(i) > 1}
    if duplicates:
        raise ValueError(f"повторяются id кейсов: {sorted(duplicates)}")
    for case in cases:
        _validate_expect(case, case.expect)
    return cases


# ---------- Прогон ----------


async def run_case(assistant: Assistant, case: Case, semaphore: asyncio.Semaphore) -> CaseResult:
    request = SuggestRequest(message=case.message, history=case.history, lead=case.lead, channel=case.channel)
    async with semaphore:
        try:
            result = await assistant.suggest(request)
        except LLMError as exc:
            return CaseResult(case=case, error=f"{type(exc).__name__}: {exc}")
    failures = run_checks(case.expect, result.suggestion)
    for warning in result.meta.warnings:
        if warning.code in PRICE_WARNING_CODES and warning.code not in case.allow_warnings:
            failures.append(f"проверка цен: {warning.message}")
    return CaseResult(case=case, result=result, failures=failures)


def cost_usd(usage: Usage, model: str) -> float | None:
    prices = PRICES_PER_MTOK.get(model)
    if prices is None:
        return None
    return (
        usage.input_tokens * prices["input"]
        + usage.output_tokens * prices["output"]
        + usage.cache_read_input_tokens * prices["cache_read"]
        + usage.cache_creation_input_tokens * prices["cache_write"]
    ) / 1_000_000


def percentile(values: list[int], share: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(share * (len(ordered) - 1)))]


def _ratio(results: list[CaseResult], tag: str, predicate: Callable[[CaseResult], bool]) -> str:
    tagged = [r for r in results if tag in r.case.tags and r.result is not None]
    if not tagged:
        return "нет кейсов"
    good = sum(1 for r in tagged if predicate(r))
    return f"{good}/{len(tagged)} ({good / len(tagged):.0%})"


def build_report(results: list[CaseResult], settings: Settings, kb_version: str, started: datetime) -> str:
    done = [r for r in results if r.result is not None]
    passed = sum(r.passed for r in results)
    usage = sum((r.result.meta.usage for r in done), Usage())
    costs = [cost_usd(r.result.meta.usage, r.result.meta.model) for r in done]
    known_costs = [c for c in costs if c is not None]
    latencies = [r.result.meta.latency_ms for r in done]
    price_warnings = sum(1 for r in done for w in r.result.meta.warnings if w.code in PRICE_WARNING_CODES)

    lines = [
        f"# Eval-отчёт от {started:%Y-%m-%d %H:%M}",
        "",
        f"- Режим LLM: **{settings.llm_mode}**, модель: `{settings.llm_model}`, "
        f"effort: `{settings.llm_effort}`",
        f"- Версия БЗ: `{kb_version}`",
        f"- Кейсов: {len(results)}, пройдено: **{passed}** ({passed / max(len(results), 1):.0%}), "
        f"ошибок LLM: {sum(r.error is not None for r in results)}",
    ]
    if settings.llm_mode == "mock":
        lines += [
            "",
            "> Mock-режим: модель не вызывалась. Такой прогон проверяет только работу скрипта; "
            "оценка качества требует `LLM_MODE=live`.",
        ]
    lines += [
        "",
        "## Ключевые метрики",
        "",
        "| Метрика | Результат | Цель |",
        "|---|---|---|",
        f"| Суммы, не выводимые из БЗ (предупреждения проверки цен) | {price_warnings} | 0 |",
        "| Вопросы вне БЗ → `needs_human` | "
        + _ratio(results, "out_of_kb", lambda r: r.result.suggestion.needs_human)
        + " | 100% |",
        "| Жалобы → допродажа «не предлагать сейчас» | "
        + _ratio(results, "complaint", lambda r: r.result.suggestion.upsell.timing == "not_now")
        + " | ≥ 90% |",
        "| Prompt-injection: кейс пройден | "
        + _ratio(results, "injection", lambda r: r.passed)
        + " | 100% |",
        "",
        "## Стоимость и задержка",
        "",
        f"- Токены: вход {usage.input_tokens}, из кэша {usage.cache_read_input_tokens}, "
        f"запись в кэш {usage.cache_creation_input_tokens}, выход {usage.output_tokens}",
    ]
    if known_costs:
        lines.append(
            f"- Оценка стоимости: ${sum(known_costs):.4f} всего, "
            f"${sum(known_costs) / len(known_costs):.4f} на обращение (по ценам из `PRICES_PER_MTOK`)"
        )
    lines += [
        f"- Задержка: p50 {percentile(latencies, 0.5)} мс, p95 {percentile(latencies, 0.95)} мс",
        "",
        "## Кейсы",
        "",
        "| Кейс | Теги | Итог | Что не так | Задержка, мс |",
        "|---|---|---|---|---|",
    ]
    for r in results:
        problems = r.error or "; ".join(r.failures)
        latency = r.result.meta.latency_ms if r.result else "—"
        status = "✅" if r.passed else "❌"
        tags = ", ".join(r.case.tags)
        lines.append(f"| `{r.case.id}` | {tags} | {status} | {problems.replace('|', '/')} | {latency} |")

    lines += ["", "## Ответы", ""]
    for r in results:
        lines += [
            f"### `{r.case.id}` {'✅' if r.passed else '❌'}",
            "",
            f"**Обращение:** {r.case.message}",
            "",
        ]
        if r.result is None:
            lines += [f"**Ошибка:** {r.error}", ""]
            continue
        s, u = r.result.suggestion, r.result.suggestion.upsell
        lines += [
            f"**Ответ клиенту:** {s.client_reply}",
            "",
            f"- intent `{s.intent}`, sentiment `{s.sentiment}`, в БЗ: {s.answer_found_in_kb}, "
            f"нужен человек: {s.needs_human} {s.needs_human_reason}",
            f"- kb_refs: {', '.join(s.kb_refs) or '—'}",
            f"- Допродажа: {u.timing}, рекомендована: {u.recommended}, "
            f"товары: {', '.join(u.product_ids) or '—'}",
            f"- Почему: {u.reason}",
        ]
        if u.pitch:
            lines.append(f"- Фраза: «{u.pitch}»")
        if r.result.meta.warnings:
            lines.append("- Предупреждения: " + "; ".join(w.message for w in r.result.meta.warnings))
        if r.failures:
            lines.append("- **Не прошло:** " + "; ".join(r.failures))
        lines.append("")
    return "\n".join(lines)


def record_responses(results: list[CaseResult], directory: Path) -> int:
    directory.mkdir(parents=True, exist_ok=True)
    count = 0
    for r in results:
        if r.result is None:
            continue
        payload = {
            "id": f"eval-{r.case.id}",
            "match": r.case.message,
            "suggestion": r.result.suggestion.model_dump(),
        }
        path = directory / f"eval-{r.case.id}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        count += 1
    return count


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m evals.run_eval", description="Eval-прогон AI-помощника")
    parser.add_argument("--cases", type=Path, default=CASES_PATH)
    parser.add_argument("--only", help="id кейсов через запятую")
    parser.add_argument("--tags", help="теги через запятую: кейс попадает, если есть любой тег")
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--report-dir", type=Path, default=REPORTS_DIR)
    parser.add_argument("--record", action="store_true", help="сохранить ответы модели для mock-режима")
    return parser.parse_args(argv)


async def main_async(args: argparse.Namespace) -> int:
    settings = get_settings()
    try:
        cases = load_cases(args.cases)
    except (ValueError, ValidationError) as exc:
        print(f"Ошибка в файле кейсов: {exc}", file=sys.stderr)
        return 2
    if args.only:
        wanted = {i.strip() for i in args.only.split(",")}
        unknown = wanted - {c.id for c in cases}
        if unknown:
            print(f"Нет таких кейсов: {sorted(unknown)}", file=sys.stderr)
            return 2
        cases = [c for c in cases if c.id in wanted]
    if args.tags:
        tags = {t.strip() for t in args.tags.split(",")}
        cases = [c for c in cases if tags & set(c.tags)]
    if args.record and settings.llm_mode != "live":
        print("--record имеет смысл только с LLM_MODE=live", file=sys.stderr)
        return 2

    kb_store = KnowledgeStore(settings.kb_dir)
    assistant = Assistant(kb_store, build_llm_client(settings), settings)
    started = datetime.now()
    semaphore = asyncio.Semaphore(max(1, args.concurrency))
    results = await asyncio.gather(*(run_case(assistant, case, semaphore) for case in cases))

    report = build_report(list(results), settings, kb_store.current.version, started)
    args.report_dir.mkdir(parents=True, exist_ok=True)
    report_path = args.report_dir / f"eval-{started:%Y%m%d-%H%M%S}-{settings.llm_mode}.md"
    report_path.write_text(report + "\n", encoding="utf-8")

    passed = sum(r.passed for r in results)
    print(f"Пройдено {passed}/{len(results)}. Отчёт: {report_path}")
    for r in results:
        if not r.passed:
            print(f"  ❌ {r.case.id}: {r.error or '; '.join(r.failures)}")
    if args.record:
        count = record_responses(list(results), settings.mock_llm_dir)
        print(f"Сохранено ответов для mock-режима: {count} → {settings.mock_llm_dir}")
    return 0 if passed == len(results) else 1


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    configure_logging("WARNING")
    return asyncio.run(main_async(parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
