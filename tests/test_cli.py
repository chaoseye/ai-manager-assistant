import json

import pytest

from app import cli
from app.config import BASE_DIR

EXAMPLES = BASE_DIR / "examples"
MESSAGE = "20 метров. Сколько с установкой и когда приедете? У нас ребёнок-аллергик, важно, чтобы было чисто"


@pytest.fixture(autouse=True)
def mock_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_MODE", "mock")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "cli.db"))
    cli.get_settings.cache_clear()
    yield
    cli.get_settings.cache_clear()


def test_text_output(capsys):
    code = cli.main(
        [
            MESSAGE,
            "--history",
            str(EXAMPLES / "dialog.json"),
            "--lead",
            str(EXAMPLES / "lead.json"),
            "--verbose",
        ]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "=== Ответ клиенту ===" in out
    assert "32 900 ₽" in out
    assert "=== Допродажа (только для менеджера) · сейчас ===" in out
    assert "service-1y" in out
    assert "LLM: mock" in out


def test_json_output(capsys):
    assert cli.main(["Сколько длится монтаж?", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["suggestion"]["kb_refs"] == ["faq-install-duration"]
    assert data["meta"]["llm_mode"] == "mock"


def test_stdin(monkeypatch, capsys):
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO("Сколько длится монтаж?"))
    assert cli.main(["--stdin", "--json"]) == 0
    assert "faq-install-duration" in capsys.readouterr().out


def test_bad_input_exit_code(tmp_path, capsys):
    assert cli.main([]) == cli.EXIT_BAD_INPUT
    bad = tmp_path / "history.json"
    bad.write_text("{не json", encoding="utf-8")
    assert cli.main(["?", "--history", str(bad)]) == cli.EXIT_BAD_INPUT
    wrong = tmp_path / "wrong.json"
    wrong.write_text('[{"role": "boss", "text": "x"}]', encoding="utf-8")
    assert cli.main(["?", "--history", str(wrong)]) == cli.EXIT_BAD_INPUT
    assert cli.main(["?", "--lead", str(tmp_path / "missing.json")]) == cli.EXIT_BAD_INPUT
    assert "Ошибка" in capsys.readouterr().err


def test_llm_error_exit_code(monkeypatch, capsys):
    from app.core.llm import LLMUnavailableError
    from tests.conftest import FakeLLM

    monkeypatch.setattr(cli, "build_llm_client", lambda settings: FakeLLM(LLMUnavailableError("нет сети")))
    assert cli.main(["?"]) == cli.EXIT_LLM
    assert "нет сети" in capsys.readouterr().err
