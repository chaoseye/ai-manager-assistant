"""Контекст для подсказки из amoCRM: какая сделка, её этап, товары, имя клиента, куда писать примечание."""

from collections import defaultdict
from dataclasses import dataclass

from app.amocrm.client import SYSTEM_STATUS_NAMES, AmoClient, AmoLead, NoteEntity
from app.amocrm.webhooks import ELEMENT_CONTACT, ELEMENT_LEAD
from app.core.schemas import LeadContext

MAX_TEXT = 200
MAX_ITEMS = 50


@dataclass(frozen=True)
class ResolvedContext:
    lead: LeadContext | None
    note_target: tuple[NoteEntity, int] | None


def pick_lead(leads: list[AmoLead]) -> AmoLead | None:
    """Открытая сделка, обновлённая последней. Если открытых нет — последняя закрытая:
    клиент может писать уже после покупки."""
    if not leads:
        return None
    open_leads = [lead for lead in leads if not lead.closed]
    return max(open_leads or leads, key=lambda lead: (lead.updated_at, lead.id))


def _clip(text: str | None) -> str | None:
    text = (text or "").strip()
    return text[:MAX_TEXT] or None


async def resolve_context(
    amo: AmoClient,
    *,
    element_type: int | None,
    element_id: int | None,
    contact_id: int | None,
    fallback_name: str | None,
) -> ResolvedContext:
    """Сделка берётся из привязки чата; если чат привязан к контакту — из сделок контакта."""
    lead: AmoLead | None = None
    contact_name: str | None = None

    if element_type == ELEMENT_LEAD and element_id:
        lead = await amo.get_lead(element_id)
    if element_type == ELEMENT_CONTACT and element_id:
        contact_id = contact_id or element_id

    if lead is None and contact_id:
        contact = await amo.get_contact(contact_id, with_leads=True)
        if contact is not None:
            contact_name = contact.name or None
            lead = pick_lead(await amo.get_leads(list(contact.lead_ids)))
        else:
            contact_id = None

    if lead is not None and contact_name is None and lead.main_contact_id:
        contact = await amo.get_contact(lead.main_contact_id)
        contact_name = contact.name if contact and contact.name else None
        contact_id = contact_id or lead.main_contact_id

    if lead is None:
        target: tuple[NoteEntity, int] | None = ("contacts", contact_id) if contact_id else None
        return ResolvedContext(lead=None, note_target=target)

    pipelines = await amo.get_pipelines()
    pipeline = pipelines.get(lead.pipeline_id) if lead.pipeline_id else None
    stage = pipeline.statuses.get(lead.status_id) if pipeline and lead.status_id else None
    stage = stage or SYSTEM_STATUS_NAMES.get(lead.status_id or 0)

    by_catalog: dict[int, list[int]] = defaultdict(list)
    for catalog_id, element in lead.catalog_elements:
        by_catalog[catalog_id].append(element)
    products: list[str] = []
    for catalog_id, elements in by_catalog.items():
        names = await amo.get_catalog_element_names(catalog_id, elements)
        products += [names[e] for e in elements if names.get(e)]

    context = LeadContext(
        id=lead.id,
        pipeline=_clip(pipeline.name if pipeline else None),
        stage=_clip(stage),
        budget=lead.price or None,
        products=[p[:MAX_TEXT] for p in products[:MAX_ITEMS]],
        tags=[t[:MAX_TEXT] for t in lead.tags[:MAX_ITEMS]],
        contact_name=_clip(contact_name or fallback_name),
    )
    return ResolvedContext(lead=context, note_target=("leads", lead.id))
