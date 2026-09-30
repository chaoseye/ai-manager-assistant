"""python -m app.amocrm.connect: разбор адреса, проверка токена на поддельном amoCRM, запись в .env."""

import io

import pytest

from app.amocrm import connect
from app.amocrm.fake import FakeAmoApi
from app.config import BASE_DIR, Settings

SEED = BASE_DIR / "examples" / "amocrm" / "mock_account.json"
TOKEN = "long-lived-token-0123456789abcdef"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("mycompany", ("mycompany", None)),
        ("mycompany.amocrm.ru", ("mycompany", None)),
        ("https://mycompany.amocrm.ru/leads/pipeline/1234", ("mycompany", None)),
        ("MyCompany.amocrm.ru", ("mycompany", None)),
        ("mycompany.kommo.com", ("mycompany", "https://mycompany.kommo.com")),
    ],
)
def test_parse_account(value, expected):
    assert connect.parse_account(value) == expected


@pytest.mark.parametrize("value", ["", "<поддомен>", "https://", "моя компания"])
def test_parse_account_rejects_garbage(value):
    with pytest.raises(ValueError):
        connect.parse_account(value)


@pytest.fixture
def env_file(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        "LLM_MODE=live\nAMOCRM_MODE=mock\nWEBHOOK_SECRET=dev-secret\nAMOCRM_TOKEN=\n", encoding="utf-8"
    )
    return path


def amo(token=TOKEN):
    return FakeAmoApi.from_file(SEED, token=token).transport()


def type_in(monkeypatch, text):
    monkeypatch.setattr("sys.stdin", io.StringIO(text))  # не TTY — токен читается из stdin


def test_connect_saves_checked_settings(env_file, monkeypatch, capsys):
    type_in(monkeypatch, TOKEN + "\n")
    code = connect.main(
        ["--account", "https://mycompany.amocrm.ru/leads", "--env-file", str(env_file)], transport=amo()
    )
    out = capsys.readouterr().out
    assert code == connect.EXIT_OK
    assert "Токен принят" in out and TOKEN not in out  # токен не печатается

    settings = Settings(_env_file=env_file)
    assert settings.amocrm_mode == "live" and settings.amocrm_subdomain == "mycompany"
    assert settings.amocrm_base_url is None and settings.amocrm_token == TOKEN
    assert settings.amocrm_account_id == 29000001  # id из поддельного аккаунта
    assert (
        settings.webhook_secret
        and settings.webhook_secret != "dev-secret"
        and len(settings.webhook_secret) >= 24
    )
    assert settings.webhook_secret not in out
    assert settings.llm_mode == "live"  # остальное в .env не тронуто


def test_wrong_token_saves_nothing(env_file, monkeypatch, capsys):
    before = env_file.read_text(encoding="utf-8")
    type_in(monkeypatch, "wrong-token\n")
    code = connect.main(["--account", "mycompany", "--env-file", str(env_file)], transport=amo())
    assert code == connect.EXIT_CHECK_FAILED
    assert "не принял токен" in capsys.readouterr().err
    assert env_file.read_text(encoding="utf-8") == before


def test_enter_keeps_token_and_strong_secret(env_file, monkeypatch):
    env_file.write_text(
        f"AMOCRM_MODE=live\nAMOCRM_SUBDOMAIN=mycompany\nAMOCRM_TOKEN={TOKEN}\nWEBHOOK_SECRET=strong-secret-xyz\n",
        encoding="utf-8",
    )
    type_in(monkeypatch, "\n")
    assert (
        connect.main(["--env-file", str(env_file)], transport=amo()) == connect.EXIT_OK
    )  # адрес — сохранённый
    settings = Settings(_env_file=env_file)
    assert settings.amocrm_token == TOKEN and settings.webhook_secret == "strong-secret-xyz"


def test_kommo_account_uses_base_url(env_file, monkeypatch):
    type_in(monkeypatch, TOKEN)
    assert (
        connect.main(["--account", "mycompany.kommo.com", "--env-file", str(env_file)], transport=amo()) == 0
    )
    assert Settings(_env_file=env_file).amocrm_api_base == "https://mycompany.kommo.com"


def test_bad_input(env_file, monkeypatch, capsys):
    type_in(monkeypatch, TOKEN)
    assert connect.main(["--env-file", str(env_file)]) == connect.EXIT_BAD_INPUT  # адреса нет нигде
    type_in(monkeypatch, "")
    assert connect.main(["--account", "mycompany", "--env-file", str(env_file)]) == connect.EXIT_BAD_INPUT
    assert "Ошибка" in capsys.readouterr().err
