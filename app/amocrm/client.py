"""Клиент API amoCRM v4.

Авторизация — долгосрочный токен приватной интеграции (заголовок Authorization: Bearer).
Лимит amoCRM — 7 запросов/с на интеграцию; клиент держит AMOCRM_RPS (по умолчанию 5) и повторяет
запросы на 429, 5xx и сетевых ошибках с растущей паузой.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Literal

import httpx

logger = logging.getLogger(__name__)

STATUS_WON = 142
STATUS_LOST = 143
CLOSED_STATUSES = frozenset({STATUS_WON, STATUS_LOST})
SYSTEM_STATUS_NAMES = {STATUS_WON: "Успешно реализовано", STATUS_LOST: "Закрыто и не реализовано"}
PIPELINES_TTL_SECONDS = 3600
MAX_IDS_PER_REQUEST = 50
EVENTS_PAGE_LIMIT = 100  # больше amoCRM за раз не отдаёт
EVENTS_MAX_PAGES = 5

NoteEntity = Literal["leads", "contacts"]


class AmoError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class AmoAuthError(AmoError):
    """401/403: токен неверный, просрочен или без прав."""


class AmoUnavailableError(AmoError):
    """Сеть, 429 или 5xx после всех повторов."""


class AmoRequestError(AmoError):
    """Прочие ошибки запроса (400, 402, 404 на запись и т. п.) — повтор не поможет."""


@dataclass(frozen=True)
class AmoLead:
    id: int
    name: str
    price: int
    pipeline_id: int | None
    status_id: int | None
    updated_at: int
    contact_ids: tuple[int, ...]
    main_contact_id: int | None
    tags: tuple[str, ...]
    catalog_elements: tuple[tuple[int, int], ...]  # (catalog_id, element_id)

    @property
    def closed(self) -> bool:
        return self.status_id in CLOSED_STATUSES


@dataclass(frozen=True)
class AmoContact:
    id: int
    name: str
    lead_ids: tuple[int, ...]


@dataclass(frozen=True)
class AmoPipeline:
    id: int
    name: str
    statuses: dict[int, str]


@dataclass(frozen=True)
class AmoChatEvent:
    """Запись журнала событий о сообщении в чате. Текста в ней нет — только id сообщения и беседа."""

    message_id: str
    talk_id: str | None
    entity_type: str | None
    entity_id: int | None
    created_by: int  # id сотрудника; 0 — бот или интеграция
    created_at: int  # Unix-время

    @property
    def by_user(self) -> bool:
        return self.created_by > 0


class RateLimiter:
    """Не чаще rps запросов в секунду: запросы стартуют с интервалом 1/rps."""

    def __init__(self, rps: float):
        self._interval = 1.0 / rps if rps > 0 else 0.0
        self._lock = asyncio.Lock()
        self._next_at = 0.0

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            if self._next_at > now:
                await asyncio.sleep(self._next_at - now)
                now = time.monotonic()
            self._next_at = max(now, self._next_at) + self._interval


def _problem_detail(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return response.text[:300]
    if isinstance(data, dict):
        parts = [str(data.get(k)) for k in ("title", "detail") if data.get(k)]
        if data.get("validation-errors"):
            parts.append(str(data["validation-errors"])[:300])
        return "; ".join(parts) or str(data)[:300]
    return str(data)[:300]


def _chat_event_from_json(data: Any) -> AmoChatEvent | None:
    if not isinstance(data, dict):
        return None
    after = data.get("value_after")
    first = after[0] if isinstance(after, list) and after and isinstance(after[0], dict) else {}
    message = first.get("message") if isinstance(first.get("message"), dict) else {}
    message_id = str(message.get("id") or "").strip()
    if not message_id:
        return None
    try:
        created_at = int(data.get("created_at") or 0)
        created_by = int(data.get("created_by") or 0)
        entity_id = int(data["entity_id"]) if data.get("entity_id") else None
    except (TypeError, ValueError):
        return None
    talk_id = message.get("talk_id")
    return AmoChatEvent(
        message_id=message_id,
        talk_id=str(talk_id) if talk_id not in (None, "") else None,
        entity_type=str(data["entity_type"]) if data.get("entity_type") else None,
        entity_id=entity_id,
        created_by=created_by,
        created_at=created_at,
    )


def _lead_from_json(data: dict[str, Any]) -> AmoLead:
    embedded = data.get("_embedded") or {}
    contacts = [c for c in embedded.get("contacts") or [] if isinstance(c, dict) and c.get("id")]
    main = next((int(c["id"]) for c in contacts if c.get("is_main")), None)
    elements = []
    for element in embedded.get("catalog_elements") or []:
        catalog_id = (element.get("metadata") or {}).get("catalog_id")
        if element.get("id") and catalog_id:
            elements.append((int(catalog_id), int(element["id"])))
    return AmoLead(
        id=int(data["id"]),
        name=str(data.get("name") or ""),
        price=int(data.get("price") or 0),
        pipeline_id=data.get("pipeline_id"),
        status_id=data.get("status_id"),
        updated_at=int(data.get("updated_at") or 0),
        contact_ids=tuple(int(c["id"]) for c in contacts),
        main_contact_id=main or (int(contacts[0]["id"]) if contacts else None),
        tags=tuple(str(t.get("name")) for t in embedded.get("tags") or [] if t.get("name")),
        catalog_elements=tuple(elements),
    )


class AmoClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        rps: float = 5.0,
        timeout: float = 15.0,
        max_retries: int = 3,
        backoff_seconds: float = 0.5,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._http = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            transport=transport,
            headers={"Authorization": f"Bearer {token}", "User-Agent": "ai-manager-assistant/0.1"},
        )
        self._limiter = RateLimiter(rps)
        self._max_retries = max_retries
        self._backoff = backoff_seconds
        self._pipelines: tuple[float, dict[int, AmoPipeline]] | None = None
        self._catalog_names: dict[tuple[int, int], str] = {}

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Any = None,
        json: Any = None,
        not_found_ok: bool = False,
    ) -> Any:
        """JSON ответа; None для 204 и (если not_found_ok) для 404."""
        error: AmoError = AmoUnavailableError("amoCRM недоступен")
        for attempt in range(self._max_retries + 1):
            await self._limiter.acquire()
            delay = self._backoff * (2**attempt)
            try:
                response = await self._http.request(method, path, params=params, json=json)
            except httpx.TransportError as exc:
                error = AmoUnavailableError(f"amoCRM недоступен: {type(exc).__name__}: {exc}")
            else:
                status = response.status_code
                if status == 204 or (status == 404 and not_found_ok):
                    return None
                if status < 300:
                    return response.json() if response.content else None
                detail = _problem_detail(response)
                if status in (401, 403):
                    raise AmoAuthError(f"amoCRM отклонил токен ({status}): {detail}", status)
                if status != 429 and status < 500:
                    raise AmoRequestError(f"amoCRM вернул {status} на {method} {path}: {detail}", status)
                error = AmoUnavailableError(f"amoCRM вернул {status} на {method} {path}: {detail}", status)
                retry_after = response.headers.get("retry-after", "")
                if retry_after.isdigit():
                    delay = max(delay, float(retry_after))
            if attempt < self._max_retries:
                logger.warning(
                    "amocrm_retry",
                    extra={"fields": {"path": path, "attempt": attempt + 1, "error": str(error)}},
                )
                await asyncio.sleep(delay)
        raise error

    # ---------- Чтение ----------

    async def get_account(self) -> dict[str, Any]:
        return await self._request("GET", "/api/v4/account") or {}

    async def get_lead(self, lead_id: int) -> AmoLead | None:
        data = await self._request(
            "GET", f"/api/v4/leads/{lead_id}", params={"with": "contacts,catalog_elements"}, not_found_ok=True
        )
        return _lead_from_json(data) if data else None

    async def get_leads(self, lead_ids: list[int]) -> list[AmoLead]:
        ids = list(dict.fromkeys(lead_ids))[:MAX_IDS_PER_REQUEST]
        if not ids:
            return []
        params = [("filter[id][]", str(i)) for i in ids] + [
            ("with", "contacts,catalog_elements"),
            ("limit", str(len(ids))),
        ]
        data = await self._request("GET", "/api/v4/leads", params=params)
        leads = ((data or {}).get("_embedded") or {}).get("leads") or []
        return [_lead_from_json(lead) for lead in leads]

    async def get_contact(self, contact_id: int, *, with_leads: bool = False) -> AmoContact | None:
        params = {"with": "leads"} if with_leads else None
        data = await self._request("GET", f"/api/v4/contacts/{contact_id}", params=params, not_found_ok=True)
        if not data:
            return None
        leads = (data.get("_embedded") or {}).get("leads") or []
        return AmoContact(
            id=int(data["id"]),
            name=str(data.get("name") or "").strip(),
            lead_ids=tuple(int(lead["id"]) for lead in leads if lead.get("id")),
        )

    async def get_pipelines(self) -> dict[int, AmoPipeline]:
        now = time.monotonic()
        if self._pipelines and now - self._pipelines[0] < PIPELINES_TTL_SECONDS:
            return self._pipelines[1]
        data = await self._request("GET", "/api/v4/leads/pipelines")
        pipelines: dict[int, AmoPipeline] = {}
        for item in ((data or {}).get("_embedded") or {}).get("pipelines") or []:
            statuses = {
                int(s["id"]): str(s.get("name") or "")
                for s in (item.get("_embedded") or {}).get("statuses") or []
            }
            pipelines[int(item["id"])] = AmoPipeline(int(item["id"]), str(item.get("name") or ""), statuses)
        self._pipelines = (now, pipelines)
        return pipelines

    async def get_catalog_element_names(self, catalog_id: int, element_ids: list[int]) -> dict[int, str]:
        missing = [i for i in dict.fromkeys(element_ids) if (catalog_id, i) not in self._catalog_names]
        if missing:
            params = [("filter[id][]", str(i)) for i in missing[:MAX_IDS_PER_REQUEST]]
            data = await self._request("GET", f"/api/v4/catalogs/{catalog_id}/elements", params=params)
            for element in ((data or {}).get("_embedded") or {}).get("elements") or []:
                self._catalog_names[(catalog_id, int(element["id"]))] = str(element.get("name") or "")
        return {
            i: self._catalog_names[(catalog_id, i)]
            for i in element_ids
            if (catalog_id, i) in self._catalog_names
        }

    async def get_outgoing_chat_events(self, since: int) -> list[AmoChatEvent]:
        """Исходящие сообщения чатов всего аккаунта, записанные в журнал событий начиная с since (Unix-время).

        Вебхук о сообщении amoCRM иногда доставляет на минуты позже, а в журнале оно видно сразу. Фильтр
        журнала по сделке на события чатов не действует (ответ пустой), поэтому беседу отбирает вызывающий.
        """
        events: list[AmoChatEvent] = []
        for page in range(1, EVENTS_MAX_PAGES + 1):
            params = {
                "filter[type]": "outgoing_chat_message",
                "filter[created_at][from]": str(since),
                "limit": str(EVENTS_PAGE_LIMIT),
                "page": str(page),
            }
            data = await self._request("GET", "/api/v4/events", params=params)
            items = ((data or {}).get("_embedded") or {}).get("events") or []
            events += [event for event in map(_chat_event_from_json, items) if event is not None]
            if len(items) < EVENTS_PAGE_LIMIT:
                break
        return events

    # ---------- Запись ----------

    async def add_note(self, entity: NoteEntity, entity_id: int, text: str, service: str) -> int | None:
        """Служебное примечание (service_message) в ленту сделки или контакта. Возвращает id примечания."""
        payload = [{"note_type": "service_message", "params": {"service": service, "text": text}}]
        data = await self._request("POST", f"/api/v4/{entity}/{entity_id}/notes", json=payload)
        notes = ((data or {}).get("_embedded") or {}).get("notes") or []
        return int(notes[0]["id"]) if notes and notes[0].get("id") else None

    async def subscribe_webhook(self, destination: str, events: list[str]) -> dict[str, Any]:
        return await self._request(
            "POST", "/api/v4/webhooks", json={"destination": destination, "settings": events}
        )
