from pathlib import Path

import pytest

from app.kb.loader import KBValidationError, KnowledgeStore, load_knowledge_base, render_for_prompt
from tests.conftest import KB_DIR


def _replace(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, f"в {path.name} нет фрагмента {old!r}"
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def test_demo_kb_loads():
    kb = load_knowledge_base(KB_DIR)
    assert kb.counts() == {"products": 13, "faq": 9, "policies": 7, "upsell_rules": 8}
    assert "гарантируем 100%" in kb.forbidden_phrases
    assert "company" in kb.all_ids
    assert kb.products_by_id["ac-basic-09"].price == 32900


def test_version_is_stable_and_depends_on_content(kb_copy: Path):
    first = load_knowledge_base(kb_copy).version
    assert load_knowledge_base(kb_copy).version == first
    _replace(kb_copy / "products.yaml", "price: 32900", "price: 33900")
    assert load_knowledge_base(kb_copy).version != first


def test_render_is_sorted_and_deterministic(kb_copy: Path):
    kb = load_knowledge_base(kb_copy)
    rendered = render_for_prompt(kb)
    # Порядок записей в файле не влияет на промпт: переставим два товара местами.
    path = kb_copy / "products.yaml"
    text = path.read_text(encoding="utf-8")
    blocks = text.split("\n- id: ")
    header, items = blocks[0], blocks[1:]
    items[0], items[1] = items[1], items[0]
    path.write_text("\n- id: ".join([header, *items]), encoding="utf-8")
    assert render_for_prompt(load_knowledge_base(kb_copy)) == rendered
    assert rendered.index('id="ac-basic-07"') < rendered.index('id="ac-basic-09"')
    assert "Цена: 32 900 ₽" in rendered
    assert "Цена: 1 500 ₽ за метр" in rendered


@pytest.mark.parametrize(
    ("file_name", "old", "new", "expected"),
    [
        ("products.yaml", "price: 32900", 'price: "32 900"', "id=ac-basic-09), поле «price»"),
        ("products.yaml", "id: ac-basic-12", "id: ac-basic-09", "id «ac-basic-09» уже используется"),
        ("products.yaml", "id: ac-basic-12", "id: AC_12", "поле «id»"),
        ("upsell_rules.yaml", "offer: [service-1y]", "offer: [service-2y]", "нет товара «service-2y»"),
        (
            "upsell_rules.yaml",
            "  discount_condition: при заказе вместе с монтажом\n",
            "",
            "discount_condition",
        ),
        ("faq.yaml", "- id: faq-noise", "- id: faq-noise\n  unknown_field: 1", "unknown_field"),
        ("tone_of_voice.md", "## Запрещённые фразы", "## Прочее", "Запрещённые фразы"),
        ("policies.yaml", "- id: pol-payment", "- id: pol-payment\n  title: [", "ошибка YAML"),
    ],
)
def test_invalid_kb_reports_file_record_and_field(kb_copy: Path, file_name, old, new, expected):
    _replace(kb_copy / file_name, old, new)
    with pytest.raises(KBValidationError) as exc_info:
        load_knowledge_base(kb_copy)
    joined = "\n".join(exc_info.value.errors)
    assert file_name in joined
    assert expected in joined


def test_missing_file_and_missing_dir(kb_copy: Path, tmp_path: Path):
    (kb_copy / "faq.yaml").unlink()
    with pytest.raises(KBValidationError, match=r"faq\.yaml: файл не найден"):
        load_knowledge_base(kb_copy)
    with pytest.raises(KBValidationError, match="каталог базы знаний не найден"):
        load_knowledge_base(tmp_path / "nope")


def test_all_errors_are_reported_at_once(kb_copy: Path):
    _replace(kb_copy / "products.yaml", "price: 32900", "price: -1")
    _replace(kb_copy / "upsell_rules.yaml", "offer: [wifi-module]", "offer: [wifi-9000]")
    with pytest.raises(KBValidationError) as exc_info:
        load_knowledge_base(kb_copy)
    assert len(exc_info.value.errors) == 2


def test_reload_keeps_previous_version_on_error(kb_copy: Path):
    store = KnowledgeStore(kb_copy)
    version = store.current.version
    _replace(kb_copy / "products.yaml", "price: 32900", "price: 0")
    with pytest.raises(KBValidationError):
        store.reload()
    assert store.current.version == version

    _replace(kb_copy / "products.yaml", "price: 0", "price: 31900")
    kb, changed = store.reload()
    assert changed and kb.products_by_id["ac-basic-09"].price == 31900
    assert store.reload()[1] is False
