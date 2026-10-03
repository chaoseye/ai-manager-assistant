"""База знаний на испорченных файлах: любая ошибка — понятный KBValidationError, прежняя версия остаётся."""

import shutil

import pytest
import yaml
from fastapi.testclient import TestClient
from hypothesis import given, settings
from hypothesis import strategies as st

from app.kb.loader import KBValidationError, load_knowledge_base
from app.main import create_app
from tests.conftest import KB_DIR, make_settings

_scalars = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(10**12), max_value=10**12),
    st.floats(allow_nan=True, allow_infinity=True),
    st.text(max_size=20),
    st.lists(st.text(max_size=5), max_size=3),
    st.dictionaries(st.text(max_size=5), st.text(max_size=5), max_size=2),
)
FIELDS = ["id", "name", "category", "price", "unit", "description", "for_whom", "related", "extra_field"]
PRODUCTS = yaml.safe_load((KB_DIR / "products.yaml").read_text(encoding="utf-8"))


@settings(max_examples=40)  # каждый пример копирует БЗ на диск
@given(
    field=st.sampled_from(FIELDS), value=_scalars, index=st.integers(min_value=0, max_value=len(PRODUCTS) - 1)
)
def test_any_field_corruption_gives_readable_error(tmp_path_factory, field, value, index):
    kb_dir = tmp_path_factory.mktemp("kb")
    shutil.copytree(KB_DIR, kb_dir, dirs_exist_ok=True)
    products = [dict(p) for p in PRODUCTS]
    products[index][field] = value
    (kb_dir / "products.yaml").write_text(yaml.safe_dump(products, allow_unicode=True), encoding="utf-8")
    try:
        load_knowledge_base(kb_dir)
    except KBValidationError as exc:
        assert exc.errors and all(isinstance(e, str) and e for e in exc.errors)


def test_duplicate_yaml_key_is_reported(kb_copy):
    path = kb_copy / "products.yaml"
    path.write_text(
        path.read_text(encoding="utf-8").replace("  price: 32900\n", "  price: 32900\n  price: 3290\n", 1),
        encoding="utf-8",
    )
    with pytest.raises(KBValidationError) as exc:
        load_knowledge_base(kb_copy)
    # Сообщение называет файл, ключ и строку — его можно исправить, не гадая.
    message = " ".join(exc.value.errors)
    assert "products.yaml" in message and "«price» повторяется" in message and "line" in message
    # Файл товаров не разобрался — правила допродаж не сыплют «нет товара …» на каждую ссылку.
    assert len(exc.value.errors) == 1


def test_same_key_in_different_records_is_fine(kb_copy):
    # Повтор ключа — только внутри одной записи; «price» у каждого товара — нормально.
    assert len(load_knowledge_base(kb_copy).products) > 1


def test_empty_forbidden_section_turns_check_off(kb_copy):
    """Наблюдение: раздел есть, но пустой — проверка стоп-фраз молча выключается."""
    path = kb_copy / "tone_of_voice.md"
    head = path.read_text(encoding="utf-8").split("## Запрещённые фразы")[0]
    path.write_text(head + "## Запрещённые фразы\n", encoding="utf-8")
    assert load_knowledge_base(kb_copy).forbidden_phrases == ()


# ---------- Перезагрузка через API после «бытовых» правок ----------


@pytest.fixture
def kb_api(tmp_path, kb_copy):
    with TestClient(
        create_app(make_settings(tmp_path, kb_dir=kb_copy)), raise_server_exceptions=False
    ) as client:
        yield client, kb_copy


def test_cp1251_file_gives_readable_error(kb_api):
    client, kb_dir = kb_api
    path = kb_dir / "faq.yaml"
    path.write_bytes(path.read_text(encoding="utf-8").encode("cp1251", errors="replace"))
    response = client.post("/api/v1/kb/reload")
    assert response.status_code == 422 and response.json()["error"]["code"] == "kb_invalid"
    errors = " ".join(response.json()["error"]["details"])
    assert "faq.yaml" in errors and "UTF-8" in errors
    # Прежняя версия БЗ продолжает работать.
    assert client.get("/health").json()["status"] == "ok"


def test_cp1251_file_at_start_lists_only_the_encoding_problem(kb_copy):
    path = kb_copy / "products.yaml"
    path.write_bytes(path.read_text(encoding="utf-8").encode("cp1251", errors="replace"))
    with pytest.raises(KBValidationError) as exc:
        load_knowledge_base(kb_copy)
    # Нечитаемый файл — одна ошибка, а не лавина «нет товара …» в правилах допродаж.
    assert len(exc.value.errors) == 1 and "UTF-8" in exc.value.errors[0]


def test_files_with_bom_load(kb_api):
    client, kb_dir = kb_api
    for name in ("faq.yaml", "tone_of_voice.md", "company.md"):
        path = kb_dir / name
        path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())
    assert client.post("/api/v1/kb/reload").status_code == 200


def test_tab_indent_keeps_previous_version(kb_api):
    client, kb_dir = kb_api
    path = kb_dir / "faq.yaml"
    path.write_text(
        path.read_text(encoding="utf-8").replace("  questions:", "\tquestions:", 1), encoding="utf-8"
    )
    response = client.post("/api/v1/kb/reload")
    assert response.status_code == 422 and "faq.yaml" in str(response.json()["error"]["details"])
    assert client.get("/health").json()["status"] == "ok"


def test_price_as_text_points_to_record_and_field(kb_api):
    client, kb_dir = kb_api
    path = kb_dir / "products.yaml"
    path.write_text(
        path.read_text(encoding="utf-8").replace("price: 32900", 'price: "32 900"', 1), encoding="utf-8"
    )
    response = client.post("/api/v1/kb/reload")
    assert response.status_code == 422
    assert any("ac-basic-09" in d and "price" in d for d in response.json()["error"]["details"])
