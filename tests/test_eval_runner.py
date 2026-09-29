"""Сам eval-скрипт: кейсы валидны, проверки работают, отчёт строится (в mock-режиме, без модели)."""

import pytest

from app.core.schemas import Usage
from evals.run_eval import CASES_PATH, Case, cost_usd, load_cases, main, run_checks
from tests.conftest import make_suggestion


def test_cases_file_is_valid():
    cases = load_cases(CASES_PATH)
    assert len(cases) >= 25
    tags = {tag for case in cases for tag in case.tags}
    assert {"kb", "out_of_kb", "complaint", "torg", "declined", "upsell", "injection"} <= tags


def test_unknown_check_is_rejected(tmp_path):
    path = tmp_path / "cases.yaml"
    path.write_text("- id: x\n  message: hi\n  expect: {reply_is_nice: true}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="reply_is_nice"):
        load_cases(path)


def test_checks():
    suggestion = make_suggestion(
        client_reply="Монтаж стоит 9 900 ₽",
        upsell={
            "recommended": True,
            "timing": "now",
            "product_ids": ["service-1y"],
            "offer": "x",
            "reason": "y",
        },
    )
    assert (
        run_checks({"reply_contains_any": ["9 900"], "upsell_products_any": ["service-1y"]}, suggestion) == []
    )
    failures = run_checks(
        {"needs_human": True, "upsell_products_none": ["service-1y"], "reply_not_contains": ["монтаж"]},
        suggestion,
    )
    assert len(failures) == 3
    assert run_checks({"any_of": [{"needs_human": True}, {"upsell_timing": ["now"]}]}, suggestion) == []
    assert (
        len(run_checks({"any_of": [{"needs_human": True}, {"upsell_timing": ["not_now"]}]}, suggestion)) == 1
    )


def test_cost():
    usage = Usage(input_tokens=1_000_000, output_tokens=100_000, cache_read_input_tokens=1_000_000)
    assert cost_usd(usage, "claude-opus-5-5") == pytest.approx(4.0 + 2.0 + 0.2)
    assert cost_usd(usage, "mock") is None


def test_mock_run_writes_report(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("LLM_MODE", "mock")
    from app.config import get_settings

    get_settings.cache_clear()
    try:
        code = main(["--only", "declined-service,upsell-after-sale", "--report-dir", str(tmp_path)])
    finally:
        get_settings.cache_clear()
    assert code == 0, capsys.readouterr().out
    reports = list(tmp_path.glob("eval-*-mock-claude.md"))
    assert len(reports) == 1
    text = reports[0].read_text(encoding="utf-8")
    assert "Mock-режим" in text and "`declined-service`" in text


def test_record_requires_live(monkeypatch, capsys):
    monkeypatch.setenv("LLM_MODE", "mock")
    from app.config import get_settings

    get_settings.cache_clear()
    try:
        assert main(["--record", "--only", "kb-payment"]) == 2
    finally:
        get_settings.cache_clear()


def test_case_model_defaults():
    case = Case(id="x", message="hi")
    assert case.expect == {} and case.allow_warnings == []


def _run_mock(monkeypatch, argv):
    monkeypatch.setenv("LLM_MODE", "mock")
    from app.config import get_settings

    get_settings.cache_clear()
    try:
        return main(argv)
    finally:
        get_settings.cache_clear()


def test_compare_all_providers(monkeypatch, tmp_path, capsys):
    code = _run_mock(
        monkeypatch, ["--provider", "all", "--only", "declined-service", "--report-dir", str(tmp_path)]
    )
    assert code == 0, capsys.readouterr().out
    reports = sorted(p.name for p in tmp_path.glob("eval-*-mock-*.md"))
    assert len(reports) == 7  # шесть моделей + сводная таблица
    compare = next(tmp_path.glob("eval-*-compare.md")).read_text(encoding="utf-8")
    assert "Сравнение моделей" in compare and "| Grok (mock) | 1/1 |" in compare
    report = next(tmp_path.glob("eval-*-kimi.md")).read_text(encoding="utf-8")
    assert "Kimi (mock)" in report


def test_unknown_provider(monkeypatch, capsys):
    assert _run_mock(monkeypatch, ["--provider", "gpt", "--only", "kb-payment"]) == 2
    assert "gpt" in capsys.readouterr().err


def test_cost_falls_back_to_configured_model():
    from datetime import UTC, datetime

    from app.core.schemas import Meta, SuggestResult
    from evals.run_eval import CaseResult, summarize

    usage = Usage(input_tokens=1_000_000, output_tokens=1_000_000)
    meta = Meta(
        suggestion_id="x",
        mode="full",
        llm_mode="live",
        provider="grok",
        model="grok-4.7-20260916",
        kb_version="v",
        latency_ms=1,
        attempts=1,
        usage=usage,
        warnings=[],
        created_at=datetime.now(UTC),
    )
    result = CaseResult(
        case=Case(id="c", message="?"), result=SuggestResult(suggestion=make_suggestion(), meta=meta)
    )
    assert summarize([result]).cost_total is None  # версию с датой прайс не знает
    assert summarize([result], "grok-4.7").cost_total == pytest.approx(2.0 + 6.0)


def test_stops_when_gateway_is_down(monkeypatch, capsys):
    from app.core import llm_registry

    async def gateway_down(*args, **kwargs):
        raise RuntimeError("шлюз недоступен: ConnectError")

    monkeypatch.setattr(llm_registry, "list_gateway_models", gateway_down)
    monkeypatch.setenv("LLM_GATEWAY_URL", "https://gw.test/v1")
    monkeypatch.setenv("LLM_GATEWAY_KEY", "k")
    monkeypatch.setenv("LLM_MODE", "live")
    from app.config import get_settings

    get_settings.cache_clear()
    try:
        assert main(["--provider", "grok", "--only", "kb-payment"]) == 2
    finally:
        get_settings.cache_clear()
    assert "шлюз LLM недоступен" in capsys.readouterr().err
