"""Схемы записей базы знаний."""

from dataclasses import dataclass, field
from functools import cached_property
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StringConstraints, model_validator

KbId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

COMPANY_ID = "company"


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Product(_Record):
    id: KbId
    name: Text
    category: Text
    price: StrictInt = Field(gt=0, description="Цена в рублях, целое число")
    unit: Text | None = Field(default=None, description="Единица, если не штука: «за метр»")
    description: Text
    for_whom: str = ""
    related: list[KbId] = Field(default_factory=list)


class FaqItem(_Record):
    id: KbId
    questions: list[Text] = Field(min_length=1)
    answer: Text


class Policy(_Record):
    id: KbId
    title: Text
    text: Text


class UpsellRule(_Record):
    id: KbId
    when: Text
    offer: list[KbId] = Field(min_length=1)
    argument: Text
    discount_percent: StrictInt | None = Field(default=None, gt=0, lt=100)
    discount_condition: Text | None = None
    not_when: str = ""

    @model_validator(mode="after")
    def _discount_needs_condition(self) -> "UpsellRule":
        if self.discount_percent is not None and not self.discount_condition:
            raise ValueError("для discount_percent нужно заполнить discount_condition")
        return self


@dataclass(frozen=True)
class KnowledgeBase:
    """Загруженная и проверенная база знаний. Записи отсортированы по id."""

    products: tuple[Product, ...]
    faq: tuple[FaqItem, ...]
    policies: tuple[Policy, ...]
    upsell_rules: tuple[UpsellRule, ...]
    company_text: str
    tone_text: str
    forbidden_phrases: tuple[str, ...]
    version: str
    _cache: dict = field(default_factory=dict, compare=False, repr=False)

    @cached_property
    def products_by_id(self) -> dict[str, Product]:
        return {product.id: product for product in self.products}

    @cached_property
    def all_ids(self) -> frozenset[str]:
        ids = {COMPANY_ID}
        for group in (self.products, self.faq, self.policies, self.upsell_rules):
            ids.update(record.id for record in group)
        return frozenset(ids)

    def counts(self) -> dict[str, int]:
        return {
            "products": len(self.products),
            "faq": len(self.faq),
            "policies": len(self.policies),
            "upsell_rules": len(self.upsell_rules),
        }
