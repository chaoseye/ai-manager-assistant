"""Просмотр и перезагрузка базы знаний."""

from typing import Any

from fastapi import APIRouter, Depends

from app.api.deps import get_kb_store, require_admin_token
from app.kb.loader import KnowledgeStore

router = APIRouter(prefix="/api/v1/kb", tags=["kb"], dependencies=[Depends(require_admin_token)])


@router.get("", summary="Текущая версия и содержимое базы знаний")
async def get_kb(kb_store: KnowledgeStore = Depends(get_kb_store)) -> dict[str, Any]:
    kb = kb_store.current
    return {
        "version": kb.version,
        "counts": kb.counts(),
        "company": kb.company_text,
        "products": [p.model_dump() for p in kb.products],
        "faq": [f.model_dump() for f in kb.faq],
        "policies": [p.model_dump() for p in kb.policies],
        "upsell_rules": [r.model_dump() for r in kb.upsell_rules],
        "forbidden_phrases": list(kb.forbidden_phrases),
    }


@router.post("/reload", summary="Перечитать файлы базы знаний без рестарта")
async def reload_kb(kb_store: KnowledgeStore = Depends(get_kb_store)) -> dict[str, Any]:
    # KBValidationError обрабатывается глобально (422 kb_invalid); прежняя версия остаётся.
    kb, changed = kb_store.reload()
    return {"version": kb.version, "changed": changed, "counts": kb.counts()}
