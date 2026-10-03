"""Загрузка, проверка и вывод в промпт базы знаний."""

import hashlib
import logging
import re
from pathlib import Path
from typing import Any, TypeVar

import yaml
from pydantic import BaseModel, ValidationError

from app.core.money import format_rub
from app.kb.models import COMPANY_ID, FaqItem, KnowledgeBase, Policy, Product, UpsellRule

logger = logging.getLogger(__name__)

COMPANY_FILE = "company.md"
PRODUCTS_FILE = "products.yaml"
FAQ_FILE = "faq.yaml"
POLICIES_FILE = "policies.yaml"
UPSELL_FILE = "upsell_rules.yaml"
TONE_FILE = "tone_of_voice.md"
ALL_FILES = (COMPANY_FILE, PRODUCTS_FILE, FAQ_FILE, POLICIES_FILE, UPSELL_FILE, TONE_FILE)

_FORBIDDEN_HEADING_RE = re.compile(r"^#{1,6}\s*запрещ[её]нные\s+фразы\s*$", re.IGNORECASE)

RecordT = TypeVar("RecordT", bound=BaseModel)


class KBValidationError(Exception):
    """База знаний не прошла проверку. errors — список понятных сообщений."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("База знаний не прошла проверку:\n" + "\n".join(f"- {e}" for e in errors))


class _UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader, который не молчит о повторе ключа: «price» дважды в записи — ошибка с номером строки,
    а не тихо взятое последнее значение."""

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        self.flatten_mapping(node)
        seen: set[Any] = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicate = key in seen
            except TypeError:  # нехешируемый ключ — о нём скажет сам SafeLoader
                continue
            if duplicate:
                raise yaml.constructor.ConstructorError(
                    "в записи", node.start_mark, f"ключ «{key}» повторяется", key_node.start_mark
                )
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def _parse_records(
    file_name: str, raw: str, model: type[RecordT], errors: list[str]
) -> tuple[list[RecordT], set[str] | None]:
    """Возвращает валидные записи и id всех записей файла — в том числе тех, где ошибка в другом поле.
    По вторым проверяются ссылки: иначе одна ошибка в цене порождает лавину «нет товара …». None вместо id —
    файл не разобрался целиком (ошибка YAML), и ссылки на его записи проверять не с чем."""
    try:
        data = yaml.load(raw, Loader=_UniqueKeyLoader)
    except yaml.YAMLError as exc:
        errors.append(f"{file_name}: ошибка YAML: {exc}")
        return [], None
    if data is None:
        return [], set()
    if not isinstance(data, list):
        errors.append(f"{file_name}: на верхнем уровне должен быть список записей")
        return [], None

    records: list[RecordT] = []
    declared_ids: set[str] = set()
    for index, item in enumerate(data, start=1):
        if not isinstance(item, dict):
            errors.append(f"{file_name}: запись #{index} должна быть словарём")
            continue
        if isinstance(item.get("id"), str):
            declared_ids.add(item["id"])
        try:
            records.append(model.model_validate(item))
        except ValidationError as exc:
            record_id = item.get("id", "?")
            for error in exc.errors():
                location = ".".join(str(part) for part in error["loc"]) or "запись"
                errors.append(
                    f"{file_name}: запись #{index} (id={record_id}), поле «{location}»: {error['msg']}"
                )
    return records, declared_ids


def _parse_forbidden_phrases(raw: str, errors: list[str]) -> tuple[str, ...]:
    lines = raw.splitlines()
    start = next((i for i, line in enumerate(lines) if _FORBIDDEN_HEADING_RE.match(line.strip())), None)
    if start is None:
        errors.append(f"{TONE_FILE}: нет обязательного раздела «## Запрещённые фразы»")
        return ()
    phrases: list[str] = []
    for line in lines[start + 1 :]:
        stripped = line.strip()
        if stripped.startswith("#"):
            break
        if stripped.startswith(("- ", "* ")):
            phrase = stripped[2:].strip()
            if phrase:
                phrases.append(phrase)
    return tuple(phrases)


def load_knowledge_base(kb_dir: Path) -> KnowledgeBase:
    """Читает и проверяет все файлы БЗ. При любой ошибке — KBValidationError со всеми ошибками сразу."""
    errors: list[str] = []
    raw: dict[str, str] = {}
    if not kb_dir.is_dir():
        raise KBValidationError([f"каталог базы знаний не найден: {kb_dir}"])
    for file_name in ALL_FILES:
        path = kb_dir / file_name
        if not path.is_file():
            errors.append(f"{file_name}: файл не найден")
            continue
        try:
            raw[file_name] = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            # Блокнот и Excel в Windows сохраняют в cp1251: без этого перезагрузка падала с 500.
            errors.append(
                f"{file_name}: файл не в кодировке UTF-8 (байт {exc.start}) — пересохраните его в UTF-8"
            )

    products, declared_product_ids = _parse_records(
        PRODUCTS_FILE, raw.get(PRODUCTS_FILE, ""), Product, errors
    )
    if PRODUCTS_FILE not in raw:
        declared_product_ids = None  # файла нет или он не в UTF-8 — об этом уже сказано выше
    faq, _ = _parse_records(FAQ_FILE, raw.get(FAQ_FILE, ""), FaqItem, errors)
    policies, _ = _parse_records(POLICIES_FILE, raw.get(POLICIES_FILE, ""), Policy, errors)
    rules, _ = _parse_records(UPSELL_FILE, raw.get(UPSELL_FILE, ""), UpsellRule, errors)

    company_text = raw.get(COMPANY_FILE, "").strip()
    if COMPANY_FILE in raw and not company_text:
        errors.append(f"{COMPANY_FILE}: файл пустой")
    tone_text = raw.get(TONE_FILE, "").strip()
    forbidden = _parse_forbidden_phrases(tone_text, errors) if TONE_FILE in raw else ()
    if PRODUCTS_FILE in raw and not products and not any(e.startswith(PRODUCTS_FILE) for e in errors):
        errors.append(f"{PRODUCTS_FILE}: нет ни одного товара")

    # id уникальны во всей БЗ: на них ссылаются kb_refs.
    seen: dict[str, str] = {COMPANY_ID: COMPANY_FILE}
    groups: list[tuple[str, list[Any]]] = [
        (PRODUCTS_FILE, products),
        (FAQ_FILE, faq),
        (POLICIES_FILE, policies),
        (UPSELL_FILE, rules),
    ]
    for file_name, records in groups:
        for record in records:
            if record.id in seen:
                errors.append(f"{file_name}: id «{record.id}» уже используется в {seen[record.id]}")
            else:
                seen[record.id] = file_name

    # Ссылки на товары — только если файл товаров прочитан: иначе каждая ссылка «битая», и одна ошибка
    # кодировки или YAML порождает лавину «нет товара …».
    if declared_product_ids is not None:
        for product in products:
            for ref in product.related:
                if ref not in declared_product_ids:
                    errors.append(
                        f"{PRODUCTS_FILE}: товар «{product.id}», поле «related»: нет товара «{ref}»"
                    )
        for rule in rules:
            for ref in rule.offer:
                if ref not in declared_product_ids:
                    errors.append(f"{UPSELL_FILE}: правило «{rule.id}», поле «offer»: нет товара «{ref}»")

    if errors:
        raise KBValidationError(errors)

    digest = hashlib.sha256()
    for file_name in ALL_FILES:
        digest.update(file_name.encode())
        digest.update(b"\0")
        digest.update(raw[file_name].encode("utf-8"))
        digest.update(b"\0")

    return KnowledgeBase(
        products=tuple(sorted(products, key=lambda r: r.id)),
        faq=tuple(sorted(faq, key=lambda r: r.id)),
        policies=tuple(sorted(policies, key=lambda r: r.id)),
        upsell_rules=tuple(sorted(rules, key=lambda r: r.id)),
        company_text=company_text,
        tone_text=tone_text,
        forbidden_phrases=forbidden,
        version=digest.hexdigest()[:12],
    )


class KnowledgeStore:
    """Текущая версия БЗ с атомарной перезагрузкой: при ошибке остаётся прежняя версия."""

    def __init__(self, kb_dir: Path):
        self._kb_dir = kb_dir
        self._kb = load_knowledge_base(kb_dir)
        logger.info("kb_loaded", extra={"fields": {"kb_version": self._kb.version, **self._kb.counts()}})

    @property
    def current(self) -> KnowledgeBase:
        return self._kb

    def reload(self) -> tuple[KnowledgeBase, bool]:
        """Перечитывает файлы. Возвращает (БЗ, изменилась ли версия)."""
        new_kb = load_knowledge_base(self._kb_dir)
        changed = new_kb.version != self._kb.version
        self._kb = new_kb
        logger.info("kb_reloaded", extra={"fields": {"kb_version": new_kb.version, "changed": changed}})
        return new_kb, changed


# ---------- Вывод в промпт ----------


def _product_block(product: Product) -> str:
    price = format_rub(product.price) + (f" {product.unit}" if product.unit else "")
    lines = [
        f"Название: {product.name}",
        f"Категория: {product.category}",
        f"Цена: {price}",
        f"Описание: {product.description}",
    ]
    if product.for_whom:
        lines.append(f"Для кого: {product.for_whom}")
    if product.related:
        lines.append("Связанные позиции: " + ", ".join(product.related))
    return "\n".join(lines)


def _rule_block(rule: UpsellRule, kb: KnowledgeBase) -> str:
    offers = []
    for product_id in rule.offer:
        product = kb.products_by_id[product_id]
        offers.append(f"{product_id} ({product.name}, {format_rub(product.price)})")
    lines = [
        f"Когда: {rule.when}",
        "Предложить: " + "; ".join(offers),
        f"Аргумент: {rule.argument}",
    ]
    if rule.discount_percent is not None:
        lines.append(f"Скидка: {rule.discount_percent}% — {rule.discount_condition}")
    if rule.not_when:
        lines.append(f"Когда нельзя: {rule.not_when}")
    return "\n".join(lines)


def _item(item_id: str, item_type: str, body: str) -> str:
    return f'<kb_item id="{item_id}" type="{item_type}">\n{body}\n</kb_item>'


def render_for_prompt(kb: KnowledgeBase) -> str:
    """Вся БЗ одним текстом. Порядок фиксированный — это условие работы кэша промпта."""
    parts = [_item(COMPANY_ID, "company", kb.company_text)]
    parts += [_item(p.id, "product", _product_block(p)) for p in kb.products]
    parts += [
        _item(f.id, "faq", "Вопросы: " + "; ".join(f.questions) + f"\nОтвет: {f.answer}") for f in kb.faq
    ]
    parts += [_item(p.id, "policy", f"Тема: {p.title}\nТекст: {p.text}") for p in kb.policies]
    parts += [_item(r.id, "upsell_rule", _rule_block(r, kb)) for r in kb.upsell_rules]
    return "<knowledge_base>\n" + "\n".join(parts) + "\n</knowledge_base>"
