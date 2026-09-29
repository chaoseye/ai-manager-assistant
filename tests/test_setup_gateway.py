"""python -m app.setup_gateway: запись в .env, проверка ключа на поддельном шлюзе, ключ не печатается."""

import io

import httpx
import pytest

from app import setup_gateway
from app.config import Settings

URL = "https://gw.test/v1"
KEY = "sk-secret-0123456789abcdef"
NEW_API_MODELS = [
    "glm-5.3",
    "deepseek-v4-pro",
    "kimi/kimi-k3",
    "kimi-k3",
    "qwen3.8-max",
    "grok-4.7",
    "claude-opus-5",
]


def models_gateway(ids=NEW_API_MODELS, status=200, seen=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if status != 200:
            return httpx.Response(status, json={"error": {"message": "Invalid token"}})
        return httpx.Response(
            200, json={"object": "list", "data": [{"id": i, "object": "model"} for i in ids]}
        )

    return httpx.MockTransport(handler)


@pytest.fixture
def env_file(tmp_path):
    path = tmp_path / ".env"
    path.write_text("# настройки\nLLM_MODE=mock\nLLM_GATEWAY_KEY=\nAMOCRM_MODE=mock\n", encoding="utf-8")
    return path


@pytest.fixture
def key_in_stdin(monkeypatch):
    def put(text: str) -> None:
        monkeypatch.setattr("sys.stdin", io.StringIO(text))  # не TTY — ключ читается из stdin

    return put


def test_update_env_replaces_and_appends(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        "# LLM_GATEWAY_URL=commented\nLLM_MODE=mock\nexport LLM_PROVIDER=claude\n", encoding="utf-8"
    )
    setup_gateway.update_env(
        path, {"LLM_MODE": "live", "LLM_PROVIDER": "kimi", "LLM_GATEWAY_URL": URL, "LLM_GATEWAY_KEY": "a b#c"}
    )
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "# LLM_GATEWAY_URL=commented"  # комментарии не трогаем
    assert lines[1] == "LLM_MODE=live" and lines[2] == "LLM_PROVIDER=kimi"
    assert f"LLM_GATEWAY_URL={URL}" in lines
    assert "LLM_GATEWAY_KEY='a b#c'" in lines
    settings = Settings(_env_file=path)
    assert (
        settings.llm_gateway_key == "a b#c"
        and settings.llm_mode == "live"
        and settings.llm_provider == "kimi"
    )


def test_saves_after_successful_check(env_file, key_in_stdin, capsys):
    seen = []
    key_in_stdin(KEY + "\n")
    code = setup_gateway.main(
        ["--url", URL + "/", "--live", "--env-file", str(env_file)], transport=models_gateway(seen=seen)
    )
    out = capsys.readouterr().out
    assert code == setup_gateway.EXIT_OK
    assert seen[0].url == f"{URL}/models" and seen[0].headers["authorization"] == f"Bearer {KEY}"
    assert "Моделей в шлюзе: 7" in out and "✓ Kimi: kimi-k3" in out and "✗" not in out
    assert KEY not in out  # ключ не печатается

    settings = Settings(_env_file=env_file)
    assert settings.llm_gateway_url == URL and settings.llm_gateway_key == KEY and settings.llm_mode == "live"
    assert "AMOCRM_MODE=mock" in env_file.read_text(encoding="utf-8")


def test_missing_models_get_hints(env_file, key_in_stdin, capsys):
    key_in_stdin(KEY)
    ids = ["glm-5.3", "deepseek-v4-pro", "kimi/kimi-k3", "qwen3.8-max", "grok-4.7", "claude-opus-5"]
    assert setup_gateway.main(["--url", URL, "--env-file", str(env_file)], transport=models_gateway(ids)) == 0
    out = capsys.readouterr().out
    assert "✗ Kimi: «kimi-k3» нет в шлюзе — есть похожие: kimi/kimi-k3" in out
    assert "LLM_GATEWAY_MODEL_KIMI" in out
    assert "LLM_MODE=mock" in out  # подсказка, что модели пока не вызываются


def test_rejected_key_is_not_saved(env_file, key_in_stdin, capsys):
    before = env_file.read_text(encoding="utf-8")
    key_in_stdin(KEY)
    code = setup_gateway.main(
        ["--url", URL, "--env-file", str(env_file)], transport=models_gateway(status=401)
    )
    assert code == setup_gateway.EXIT_CHECK_FAILED
    assert "не принял ключ" in capsys.readouterr().err
    assert env_file.read_text(encoding="utf-8") == before


def test_no_check_and_keep_existing_key(env_file, key_in_stdin, capsys):
    key_in_stdin(KEY)
    assert setup_gateway.main(["--url", URL, "--no-check", "--env-file", str(env_file)]) == 0
    # Новый адрес туннеля, ключ прежний: пустой ввод оставляет сохранённый ключ.
    key_in_stdin("\n")
    new_url = "https://new-tunnel.test/v1"
    assert (
        setup_gateway.main(
            ["--url", new_url, "--provider", "grok", "--env-file", str(env_file)], transport=models_gateway()
        )
        == 0
    )
    settings = Settings(_env_file=env_file)
    assert (
        settings.llm_gateway_url == new_url
        and settings.llm_gateway_key == KEY
        and settings.llm_provider == "grok"
    )
    assert KEY not in capsys.readouterr().out


def test_bad_input(env_file, key_in_stdin, capsys):
    key_in_stdin(KEY)
    assert setup_gateway.main(["--env-file", str(env_file)]) == setup_gateway.EXIT_BAD_INPUT  # нет адреса
    key_in_stdin("")
    assert setup_gateway.main(["--url", URL, "--env-file", str(env_file)]) == setup_gateway.EXIT_BAD_INPUT
    assert "Ошибка" in capsys.readouterr().err


def test_wrong_url_hint(env_file, key_in_stdin, capsys):
    key_in_stdin(KEY)
    transport = httpx.MockTransport(lambda request: httpx.Response(404, text="Not Found"))
    assert (
        setup_gateway.main(["--url", "https://gw.test", "--env-file", str(env_file)], transport=transport)
        == 1
    )
    assert "/v1" in capsys.readouterr().err


def test_no_foreign_hints(env_file, key_in_stdin, capsys):
    key_in_stdin(KEY)
    ids = ["glm-5.3", "deepseek-v4-pro", "kimi-k3", "qwen3.8-max", "claude-opus-5"]
    assert setup_gateway.main(["--url", URL, "--env-file", str(env_file)], transport=models_gateway(ids)) == 0
    out = capsys.readouterr().out
    assert "✗ Grok: «grok-4.7» нет в шлюзе. Задайте LLM_GATEWAY_MODEL_GROK" in out


@pytest.mark.parametrize(
    ("url", "hint"),
    [
        ("https://<новый-адрес>.trycloudflare.com/v1", "шаблон"),
        ("https://bad host.test/v1", "некорректный адрес"),
        ("ftp://gw.test/v1", "https://"),
        ("gw.test/v1", "https://"),
    ],
)
def test_bad_url_gives_message_not_traceback(env_file, key_in_stdin, capsys, url, hint):
    before = env_file.read_text(encoding="utf-8")
    key_in_stdin(KEY)
    assert setup_gateway.main(["--url", url, "--env-file", str(env_file)], transport=models_gateway()) == 2
    assert hint in capsys.readouterr().err
    assert env_file.read_text(encoding="utf-8") == before
