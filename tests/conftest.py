import os
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck
from hypothesis import settings as hypothesis_settings

from app.config import BASE_DIR, Settings
from app.core.llm import LLMCall, LLMError, LLMResponse
from app.core.schemas import Suggestion, Upsell, Usage
from app.kb.loader import KnowledgeStore
from app.kb.models import KnowledgeBase
from app.main import create_app

KB_DIR = BASE_DIR / "knowledge_base"

# Тесты свойств (hypothesis): без ограничения по времени на пример — на медленных машинах CI оно даёт
# ложные падения. Больше примеров для локального прогона: HYPOTHESIS_MAX_EXAMPLES=1000 pytest.
hypothesis_settings.register_profile(
    "project",
    deadline=None,
    max_examples=int(os.environ.get("HYPOTHESIS_MAX_EXAMPLES", "100")),
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
hypothesis_settings.load_profile("project")


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {"llm_mode": "mock", "db_path": tmp_path / "app.db", "log_level": "WARNING"}
    values.update(overrides)
    return Settings(_env_file=None, **values)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
def kb_store() -> KnowledgeStore:
    return KnowledgeStore(KB_DIR)


@pytest.fixture
def kb(kb_store: KnowledgeStore) -> KnowledgeBase:
    return kb_store.current


@pytest.fixture
def kb_copy(tmp_path: Path) -> Path:
    """Копия демо-БЗ, которую тест может портить."""
    target = tmp_path / "kb"
    shutil.copytree(KB_DIR, target)
    return target


def make_suggestion(**overrides: object) -> Suggestion:
    upsell_overrides = overrides.pop("upsell", {})
    upsell = {
        "recommended": False,
        "timing": "not_now",
        "product_ids": [],
        "offer": "",
        "reason": "Нет повода.",
        "pitch": "",
        "avoid": "",
    }
    upsell.update(upsell_overrides)  # type: ignore[arg-type]
    values: dict[str, object] = {
        "intent": "price",
        "sentiment": "neutral",
        "client_reply": "Стандартный монтаж стоит 9 900 ₽. Когда вам удобно?",
        "kb_refs": ["install-standard"],
        "answer_found_in_kb": True,
        "needs_human": False,
        "needs_human_reason": "",
        "upsell": Upsell(**upsell),
    }
    values.update(overrides)
    return Suggestion(**values)


class FakeLLM:
    """Отдаёт заранее заданные ответы или исключения и запоминает вызовы."""

    mode = "fake"
    model = "fake-model"
    problem = None

    def __init__(self, *outcomes: Suggestion | LLMError, usage: Usage | None = None):
        self.outcomes = list(outcomes)
        self.calls: list[LLMCall] = []
        self.usage = usage or Usage(input_tokens=100, output_tokens=50)

    async def generate(self, call: LLMCall) -> LLMResponse:
        self.calls.append(call)
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, LLMError):
            raise outcome
        return LLMResponse(suggestion=outcome, model=self.model, usage=self.usage)


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as test_client:
        yield test_client
