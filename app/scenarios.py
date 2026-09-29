"""Готовые сценарии демо-страницы и имитатора amoCRM (examples/scenarios/*.json).

Модуль нарочно лёгкий — без FastAPI и SDK модели: его импортирует консольный имитатор.
"""

import json
from pathlib import Path

from pydantic import BaseModel, Field

from app.core.schemas import DialogMessage, LeadContext


class Scenario(BaseModel):
    id: str
    title: str
    description: str = ""
    channel: str | None = None
    lead: LeadContext = Field(default_factory=LeadContext)
    dialog: list[DialogMessage] = Field(min_length=1)


def load_scenarios(directory: Path) -> list[Scenario]:
    if not directory.is_dir():
        return []
    return [
        Scenario.model_validate(json.loads(path.read_text(encoding="utf-8")))
        for path in sorted(directory.glob("*.json"))
    ]
