"""Поддельный сервер amoCRM для mock-режима и тестов.

Подключается к настоящему AmoClient через httpx.MockTransport, поэтому в mock-режиме работает
весь клиентский код: заголовки, лимит запросов, повторы, разбор ответов. Отвечает в формате
API v4 (как в примерах документации) данными из JSON-файла с «аккаунтом», примечания хранит в памяти.
"""

import json
import re
import threading
import time
from pathlib import Path
from typing import Any

import httpx

_LEAD_RE = re.compile(r"^/api/v4/leads/(\d+)$")
_CONTACT_RE = re.compile(r"^/api/v4/contacts/(\d+)$")
_NOTES_RE = re.compile(r"^/api/v4/(leads|contacts)/(\d+)/notes$")
_CATALOG_RE = re.compile(r"^/api/v4/catalogs/(\d+)/elements$")


def _problem(status: int, title: str, detail: str) -> httpx.Response:
    return httpx.Response(
        status,
        json={"title": title, "type": "https://httpstatus.es/", "status": status, "detail": detail},
        headers={"Content-Type": "application/problem+json"},
    )


class FakeAmoApi:
    def __init__(self, seed: dict[str, Any], token: str):
        self.token = token
        self.account = seed.get("account", {})
        self.pipelines = seed.get("pipelines", [])
        self.catalogs = {c["id"]: c for c in seed.get("catalogs", [])}
        self.contacts = {c["id"]: c for c in seed.get("contacts", [])}
        self.leads = {lead["id"]: lead for lead in seed.get("leads", [])}
        self.notes: list[dict[str, Any]] = []
        self.webhooks: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []  # журнал событий: пока только сообщения чатов
        self.requests: list[tuple[str, str]] = []
        self._lock = threading.Lock()

    @classmethod
    def from_file(cls, path: Path, token: str) -> "FakeAmoApi":
        return cls(json.loads(path.read_text(encoding="utf-8")), token)

    # ---------- Ответы в формате API v4 ----------

    def _lead_json(self, lead: dict[str, Any], with_: set[str]) -> dict[str, Any]:
        embedded: dict[str, Any] = {
            "tags": [{"id": i + 1, "name": t} for i, t in enumerate(lead.get("tags", []))]
        }
        if "contacts" in with_:
            embedded["contacts"] = [
                {"id": cid, "is_main": i == 0} for i, cid in enumerate(lead.get("contacts", []))
            ]
        if "catalog_elements" in with_:
            embedded["catalog_elements"] = [
                {
                    "id": el["id"],
                    "metadata": {"quantity": el.get("quantity", 1), "catalog_id": el["catalog_id"]},
                }
                for el in lead.get("catalog_elements", [])
            ]
        return {
            "id": lead["id"],
            "name": lead.get("name", ""),
            "price": lead.get("price", 0),
            "pipeline_id": lead.get("pipeline_id"),
            "status_id": lead.get("status_id"),
            "updated_at": lead.get("updated_at", 0),
            "account_id": self.account.get("id"),
            "_embedded": embedded,
        }

    def _contact_json(self, contact: dict[str, Any], with_: set[str]) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": contact["id"],
            "name": contact.get("name", ""),
            "_embedded": {"tags": []},
        }
        if "leads" in with_:
            data["_embedded"]["leads"] = [{"id": lid} for lid in contact.get("leads", [])]
        return data

    # ---------- Маршрутизация ----------

    def handle(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        with self._lock:
            self.requests.append((method, path))
        if request.headers.get("authorization") != f"Bearer {self.token}":
            return _problem(401, "Unauthorized", "Неверный токен")
        with_ = set(request.url.params.get("with", "").split(",")) - {""}

        if method == "GET" and path == "/api/v4/account":
            return httpx.Response(200, json=self.account)

        if method == "GET" and path == "/api/v4/leads/pipelines":
            pipelines = [
                {"id": p["id"], "name": p["name"], "is_main": p.get("is_main", False),
                 "_embedded": {"statuses": p.get("statuses", [])}}
                for p in self.pipelines
            ]  # fmt: skip
            return httpx.Response(
                200, json={"_total_items": len(pipelines), "_embedded": {"pipelines": pipelines}}
            )

        if method == "GET" and path == "/api/v4/leads":
            ids = [int(v) for v in request.url.params.get_list("filter[id][]")]
            leads = [self._lead_json(self.leads[i], with_) for i in ids if i in self.leads]
            if not leads:
                return httpx.Response(204)
            return httpx.Response(200, json={"_embedded": {"leads": leads}})

        if method == "GET" and (m := _LEAD_RE.match(path)):
            lead = self.leads.get(int(m.group(1)))
            return httpx.Response(200, json=self._lead_json(lead, with_)) if lead else httpx.Response(204)

        if method == "GET" and (m := _CONTACT_RE.match(path)):
            contact = self.contacts.get(int(m.group(1)))
            return (
                httpx.Response(200, json=self._contact_json(contact, with_))
                if contact
                else httpx.Response(204)
            )

        if method == "GET" and (m := _CATALOG_RE.match(path)):
            catalog = self.catalogs.get(int(m.group(1)))
            if not catalog:
                return _problem(404, "Not Found", "Каталог не найден")
            ids = {int(v) for v in request.url.params.get_list("filter[id][]")}
            elements = [
                {"id": e["id"], "name": e["name"], "catalog_id": catalog["id"]}
                for e in catalog.get("elements", [])
                if not ids or e["id"] in ids
            ]
            return (
                httpx.Response(200, json={"_embedded": {"elements": elements}})
                if elements
                else httpx.Response(204)
            )

        if method == "POST" and (m := _NOTES_RE.match(path)):
            entity, entity_id = m.group(1), int(m.group(2))
            known = self.leads if entity == "leads" else self.contacts
            if entity_id not in known:
                return _problem(400, "Bad Request", f"Сущность {entity}/{entity_id} не найдена")
            payload = json.loads(request.content or b"[]")
            created = []
            with self._lock:
                for note in payload:
                    note_id = 900_000 + len(self.notes) + 1
                    self.notes.append(
                        {
                            "id": note_id,
                            "entity_type": entity,
                            "entity_id": entity_id,
                            "note_type": note.get("note_type"),
                            "params": note.get("params", {}),
                            "created_at": int(time.time()),
                        }
                    )
                    created.append({"id": note_id, "entity_id": entity_id, "request_id": str(len(created))})
            return httpx.Response(200, json={"_embedded": {"notes": created}})

        if method == "GET" and path == "/api/v4/events":
            return self._events_response(request.url.params)

        if method == "POST" and path == "/api/v4/webhooks":
            payload = json.loads(request.content or b"{}")
            with self._lock:
                self.webhooks.append(payload)
            return httpx.Response(
                200, json={**payload, "id": len(self.webhooks), "account_id": self.account.get("id")}
            )

        return _problem(404, "Not Found", f"{method} {path} не поддерживается поддельным amoCRM")

    def _events_response(self, params: httpx.QueryParams) -> httpx.Response:
        """Как настоящий журнал: новые сверху, фильтр по типу (через запятую) и времени, страницы по limit."""
        types = {t for t in params.get("filter[type]", "").split(",") if t}
        since = int(params.get("filter[created_at][from]") or 0)
        limit = min(int(params.get("limit") or 100), 100)
        page = max(int(params.get("page") or 1), 1)
        with self._lock:
            events = [
                e for e in self.events if (not types or e["type"] in types) and e["created_at"] >= since
            ]
        events.sort(key=lambda e: e["created_at"], reverse=True)
        chunk = events[(page - 1) * limit : page * limit]
        if not chunk:
            return httpx.Response(204)
        return httpx.Response(200, json={"_page": page, "_embedded": {"events": chunk}})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def add_chat_event(
        self,
        message_id: str,
        *,
        talk_id: int | str | None,
        created_at: int,
        created_by: int,
        outgoing: bool = True,
        entity_type: str = "lead",
        entity_id: int | None = None,
    ) -> None:
        """Запись журнала о сообщении в чате — в формате настоящего amoCRM (created_by 0 у бота)."""
        with self._lock:
            self.events.append(
                {
                    "id": f"evt-{len(self.events) + 1}",
                    "type": "outgoing_chat_message" if outgoing else "incoming_chat_message",
                    "entity_id": entity_id,
                    "entity_type": entity_type,
                    "created_by": created_by,
                    "created_at": created_at,
                    "value_after": [
                        {"message": {"id": message_id, "origin": "telegram", "talk_id": talk_id}}
                    ],
                    "value_before": [],
                    "account_id": self.account.get("id"),
                }
            )

    # ---------- Для просмотра в mock-режиме ----------

    def list_notes(
        self, entity_type: str | None = None, entity_id: int | None = None, after_id: int = 0
    ) -> list[dict[str, Any]]:
        with self._lock:
            notes = list(self.notes)
        return [
            n
            for n in notes
            if n["id"] > after_id
            and (entity_type is None or n["entity_type"] == entity_type)
            and (entity_id is None or n["entity_id"] == entity_id)
        ]

    def list_leads(self) -> list[dict[str, Any]]:
        """Сделки для карточки на странице «amoCRM (mock)»: воронка, этап, бюджет, контакт, теги, товары."""
        pipelines = {p["id"]: p for p in self.pipelines}
        elements = {
            (catalog_id, e["id"]): e["name"]
            for catalog_id, catalog in self.catalogs.items()
            for e in catalog.get("elements", [])
        }
        result = []
        for lead in sorted(self.leads.values(), key=lambda item: item["id"]):
            pipeline = pipelines.get(lead.get("pipeline_id"), {})
            statuses = {s["id"]: s["name"] for s in pipeline.get("statuses", [])}
            contact = self.contacts.get(lead["contacts"][0]) if lead.get("contacts") else None
            result.append(
                {
                    "id": lead["id"],
                    "name": lead.get("name", ""),
                    "pipeline": pipeline.get("name", ""),
                    "stage": statuses.get(lead.get("status_id"), ""),
                    "price": lead.get("price", 0),
                    "contact_id": contact["id"] if contact else None,
                    "contact_name": contact.get("name", "") if contact else "",
                    "tags": list(lead.get("tags", [])),
                    "products": [
                        elements.get((el["catalog_id"], el["id"]), "")
                        for el in lead.get("catalog_elements", [])
                    ],
                }
            )
        return result
