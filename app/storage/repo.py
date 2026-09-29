"""Операции с таблицей suggestions."""

import json
from typing import Any

from app.core.schemas import SuggestRequest, SuggestResult
from app.storage.db import Database


class SuggestionRepo:
    def __init__(self, db: Database):
        self._db = db

    async def save(
        self,
        result: SuggestResult,
        request: SuggestRequest,
        *,
        dialog_id: int | None = None,
        trigger_message_id: str | None = None,
    ) -> None:
        """request — уже подготовленный запрос (с замаскированными ПДн)."""
        lead_id = request.lead.id if request.lead else None
        await self._db.conn.execute(
            """
            INSERT INTO suggestions (id, dialog_id, lead_id, trigger_message_id, mode,
                                     request_json, payload_json, meta_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                result.meta.suggestion_id,
                dialog_id,
                lead_id,
                trigger_message_id,
                result.meta.mode,
                request.model_dump_json(),
                result.suggestion.model_dump_json(),
                result.meta.model_dump_json(),
                result.meta.created_at.isoformat(),
            ),
        )
        await self._db.conn.commit()

    async def get(self, suggestion_id: str) -> dict[str, Any] | None:
        cursor = await self._db.conn.execute("SELECT * FROM suggestions WHERE id = ?", (suggestion_id,))
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        return {
            "id": row["id"],
            "lead_id": row["lead_id"],
            "mode": row["mode"],
            "status": row["status"],
            "request": json.loads(row["request_json"]),
            "suggestion": json.loads(row["payload_json"]),
            "meta": json.loads(row["meta_json"]),
            "created_at": row["created_at"],
        }
