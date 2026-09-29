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
    reports = list(tmp_path.glob("eval-*-mock.md"))
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
