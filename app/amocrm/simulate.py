"""Имитатор amoCRM: шлёт в запущенный сервис вебхуки сообщений и показывает появившееся примечание.

    python -m app.amocrm.simulate --scenario price-install
    python -m app.amocrm.simulate --lead 1234 --text "Сколько стоит монтаж?"
    python -m app.amocrm.simulate --lead 1234 --chat sim-chat-1234-ab12cd --text "Спасибо!" --outgoing

Сервис должен работать с AMOCRM_MODE=mock: сделки и контакты берутся из поддельного аккаунта
(AMOCRM_MOCK_SEED), а примечания видны через /api/v1/amocrm-mock/notes.
"""

import argparse
import json
import sys
import time
import uuid

import httpx

from app.amocrm.payloads import FORM_CONTENT_TYPE, message_item, webhook_body
from app.config import get_settings
from app.scenarios import load_scenarios

SECONDS_BETWEEN_MESSAGES = 30


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.amocrm.simulate",
        description="Имитатор amoCRM: вебхуки сообщений в сервис с AMOCRM_MODE=mock.",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--scenario", help="id демо-сценария из examples/scenarios (например, price-install)")
    source.add_argument("--lead", type=int, help="id сделки из поддельного аккаунта")
    parser.add_argument("--text", help="текст сообщения (с --lead)")
    parser.add_argument("--outgoing", action="store_true", help="сообщение от менеджера, а не от клиента")
    parser.add_argument("--chat", help="id чата, чтобы продолжить уже начатый диалог")
    parser.add_argument("--url", default="http://127.0.0.1:8000", help="адрес сервиса")
    parser.add_argument("--secret", help="WEBHOOK_SECRET (по умолчанию из .env)")
    parser.add_argument("--wait", type=float, default=60, help="сколько секунд ждать примечание")
    parser.add_argument("--poll", type=float, default=1.0, help="как часто проверять примечания, с")
    return parser


def main(argv: list[str] | None = None, *, http: httpx.Client | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = make_parser()
    args = parser.parse_args(argv)
    settings = get_settings()
    secret = args.secret or settings.webhook_secret
    if not secret:
        print("Ошибка: нужен WEBHOOK_SECRET (в .env или --secret)", file=sys.stderr)
        return 2

    seed = json.loads(settings.amocrm_mock_seed.read_text(encoding="utf-8"))
    leads = {lead["id"]: lead for lead in seed["leads"]}
    contact_names = {c["id"]: c.get("name", "") for c in seed["contacts"]}

    if args.scenario:
        scenarios = {s.id: s for s in load_scenarios(settings.scenarios_dir)}
        scenario = scenarios.get(args.scenario)
        if scenario is None:
            print(f"Ошибка: нет сценария «{args.scenario}». Есть: {', '.join(scenarios)}", file=sys.stderr)
            return 2
        lead_id = scenario.lead.id
        messages = [(m.role, m.text, m.author_name) for m in scenario.dialog]
    else:
        if not args.text:
            parser.error("с --lead нужен --text")
        lead_id = args.lead
        messages = [("manager" if args.outgoing else "client", args.text, None)]

    lead = leads.get(lead_id)
    if lead is None:
        print(f"Ошибка: сделки {lead_id} нет в поддельном аккаунте. Есть: {sorted(leads)}", file=sys.stderr)
        return 2
    contact_id = lead["contacts"][0] if lead.get("contacts") else None
    client_name = contact_names.get(contact_id) or "Клиент"
    chat_id = args.chat or f"sim-chat-{lead_id}-{uuid.uuid4().hex[:6]}"
    base = args.url.rstrip("/")
    own_http = http is None
    http = http or httpx.Client(timeout=15)

    try:
        try:
            before = http.get(
                f"{base}/api/v1/amocrm-mock/notes", params={"entity_type": "leads", "entity_id": lead_id}
            )
        except httpx.TransportError:
            print(f"Ошибка: сервис недоступен по {base}. Запустите его с AMOCRM_MODE=mock.", file=sys.stderr)
            return 2
        mock_mode = before.status_code == 200
        after_id = max((n["id"] for n in before.json()), default=0) if mock_mode else 0

        print(f"Сделка #{lead_id} «{lead.get('name', '')}», чат {chat_id}")
        now = int(time.time())
        for index, (role, text, author) in enumerate(messages):
            direction = "in" if role == "client" else "out"
            name = client_name if direction == "in" else (author or "Менеджер")
            item = message_item(
                direction=direction,
                text=text,
                lead_id=lead_id,
                contact_id=contact_id,
                chat_id=chat_id,
                created_at=now - (len(messages) - 1 - index) * SECONDS_BETWEEN_MESSAGES,
                author_name=name,
            )
            response = http.post(
                f"{base}/webhooks/amocrm/{secret}",
                content=webhook_body(item, direction, seed.get("account", {})),
                headers={"Content-Type": FORM_CONTENT_TYPE},
            )
            label = "Клиент" if direction == "in" else f"Менеджер ({name})"
            print(f"  → {label}: {text}")
            if response.status_code != 200:
                print(f"Ошибка: вебхук отклонён ({response.status_code}): {response.text}", file=sys.stderr)
                return 1

        if not any(role == "client" for role, _, _ in messages):
            print("Отправлены только сообщения менеджера — подсказка не запускается.")
            return 0
        if not mock_mode:
            print("Сервис не в mock-режиме: примечание смотрите в amoCRM.")
            return 0

        print("Жду примечание AI-помощника…")
        deadline = time.monotonic() + args.wait
        while time.monotonic() < deadline:
            time.sleep(args.poll)
            notes = http.get(
                f"{base}/api/v1/amocrm-mock/notes",
                params={"entity_type": "leads", "entity_id": lead_id, "after_id": after_id},
            ).json()
            if notes:
                for note in notes:
                    params = note.get("params", {})
                    print(f"\n=== Примечание в сделке #{lead_id} · {params.get('service', '')} ===")
                    print(params.get("text", ""))
                return 0
        print(f"Примечание не появилось за {args.wait:.0f} с. Проверьте логи сервиса и /health (queue).")
        return 1
    finally:
        if own_http:
            http.close()


if __name__ == "__main__":
    sys.exit(main())
