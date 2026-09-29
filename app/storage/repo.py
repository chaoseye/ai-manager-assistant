"""Операции с таблицами: suggestions, dialogs, messages, jobs."""

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.amocrm.webhooks import ChatMessageEvent
from app.core.schemas import SuggestRequest, SuggestResult
from app.storage.db import Database, from_iso, to_iso

JOB_PENDING = "pending"
JOB_RUNNING = "running"
JOB_DONE = "done"
JOB_STALE = "stale"
JOB_SKIPPED = "skipped"
JOB_FAILED = "failed"


# ---------- Подсказки ----------


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
                to_iso(result.meta.created_at),
            ),
        )
        await self._db.conn.commit()

    async def set_note_id(self, suggestion_id: str, note_id: int | None) -> None:
        await self._db.conn.execute(
            "UPDATE suggestions SET note_id = ? WHERE id = ?", (note_id, suggestion_id)
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
            "dialog_id": row["dialog_id"],
            "lead_id": row["lead_id"],
            "trigger_message_id": row["trigger_message_id"],
            "mode": row["mode"],
            "status": row["status"],
            "note_id": row["note_id"],
            "request": json.loads(row["request_json"]),
            "suggestion": json.loads(row["payload_json"]),
            "meta": json.loads(row["meta_json"]),
            "created_at": row["created_at"],
        }


# ---------- Диалоги и сообщения ----------


@dataclass(frozen=True)
class Dialog:
    id: int
    chat_key: str
    talk_id: str | None
    contact_id: int | None
    element_type: int | None
    element_id: int | None
    origin: str | None
    contact_name: str | None


@dataclass(frozen=True)
class StoredMessage:
    amo_id: str
    direction: str  # in / out
    author_type: str  # contact / user / bot
    author_name: str | None
    text: str
    attachment_type: str | None
    created_at: datetime


class DialogRepo:
    def __init__(self, db: Database):
        self._db = db

    async def upsert(self, event: ChatMessageEvent, now: datetime) -> int:
        """Создаёт диалог или дополняет его данными из события. Возвращает id диалога."""
        contact_name = event.author_name if event.direction == "in" else None
        cursor = await self._db.conn.execute(
            """
            INSERT INTO dialogs (chat_key, talk_id, contact_id, element_type, element_id, origin,
                                 contact_name, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (chat_key) DO UPDATE SET
                talk_id      = COALESCE(excluded.talk_id, talk_id),
                contact_id   = COALESCE(excluded.contact_id, contact_id),
                element_type = COALESCE(excluded.element_type, element_type),
                element_id   = COALESCE(excluded.element_id, element_id),
                origin       = COALESCE(excluded.origin, origin),
                contact_name = COALESCE(excluded.contact_name, contact_name),
                updated_at   = excluded.updated_at
            RETURNING id
            """,
            (
                event.dialog_key,
                event.talk_id,
                event.contact_id,
                event.element_type,
                event.element_id,
                event.origin,
                contact_name,
                to_iso(now),
                to_iso(now),
            ),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return int(row[0])

    async def add_message(self, dialog_id: int, event: ChatMessageEvent, now: datetime) -> bool:
        """False, если сообщение с таким id уже есть (повторный вебхук)."""
        cursor = await self._db.conn.execute(
            """
            INSERT OR IGNORE INTO messages (amo_id, dialog_id, direction, author_type, author_name, text,
                                            attachment_type, created_at, received_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.id,
                dialog_id,
                event.direction,
                event.author_type,
                event.author_name,
                event.text,
                event.attachment_type,
                to_iso(event.created_at),
                to_iso(now),
            ),
        )
        inserted = cursor.rowcount == 1
        await cursor.close()
        return inserted

    async def get(self, dialog_id: int) -> Dialog | None:
        cursor = await self._db.conn.execute("SELECT * FROM dialogs WHERE id = ?", (dialog_id,))
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        return Dialog(
            id=row["id"],
            chat_key=row["chat_key"],
            talk_id=row["talk_id"],
            contact_id=row["contact_id"],
            element_type=row["element_type"],
            element_id=row["element_id"],
            origin=row["origin"],
            contact_name=row["contact_name"],
        )

    async def messages(self, dialog_id: int, limit: int = 200) -> list[StoredMessage]:
        """Последние limit сообщений по времени amoCRM; при равном времени — в порядке поступления."""
        cursor = await self._db.conn.execute(
            """
            SELECT * FROM (
                SELECT *, rowid AS rid FROM messages WHERE dialog_id = ?
                ORDER BY created_at DESC, rid DESC LIMIT ?
            ) ORDER BY created_at, rid
            """,
            (dialog_id, limit),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [
            StoredMessage(
                amo_id=row["amo_id"],
                direction=row["direction"],
                author_type=row["author_type"],
                author_name=row["author_name"],
                text=row["text"],
                attachment_type=row["attachment_type"],
                created_at=from_iso(row["created_at"]),
            )
            for row in rows
        ]


# ---------- Очередь ----------


@dataclass(frozen=True)
class Job:
    id: int
    dialog_id: int
    trigger_message_id: str
    run_at: datetime
    status: str
    attempts: int
    error: str | None
    suggestion_id: str | None


def _job(row: Any) -> Job:
    return Job(
        id=row["id"],
        dialog_id=row["dialog_id"],
        trigger_message_id=row["trigger_message_id"],
        run_at=from_iso(row["run_at"]),
        status=row["status"],
        attempts=row["attempts"],
        error=row["error"],
        suggestion_id=row["suggestion_id"],
    )


class JobRepo:
    def __init__(self, db: Database):
        self._db = db

    async def schedule(
        self, dialog_id: int, trigger_message_id: str, run_at: datetime, now: datetime
    ) -> None:
        """Ставит задачу диалога или сдвигает уже ожидающую (debounce)."""
        conn = self._db.conn
        for _ in range(2):
            cursor = await conn.execute(
                """
                UPDATE jobs SET run_at = ?, trigger_message_id = ?, updated_at = ?
                WHERE dialog_id = ? AND status = 'pending'
                """,
                (to_iso(run_at), trigger_message_id, to_iso(now), dialog_id),
            )
            updated = cursor.rowcount
            await cursor.close()
            if updated:
                return
            try:
                await conn.execute(
                    """
                    INSERT INTO jobs (dialog_id, trigger_message_id, run_at, status, created_at, updated_at)
                    VALUES (?, ?, ?, 'pending', ?, ?)
                    """,
                    (dialog_id, trigger_message_id, to_iso(run_at), to_iso(now), to_iso(now)),
                )
                return
            except sqlite3.IntegrityError:
                continue  # задачу успели создать параллельно — сдвигаем её

    async def claim_due(self, now: datetime, limit: int) -> list[Job]:
        """Забирает созревшие задачи в работу (pending → running, attempts + 1)."""
        conn = self._db.conn
        cursor = await conn.execute(
            "SELECT id FROM jobs WHERE status = 'pending' AND run_at <= ? ORDER BY run_at LIMIT ?",
            (to_iso(now), limit),
        )
        ids = [row["id"] for row in await cursor.fetchall()]
        await cursor.close()
        claimed: list[Job] = []
        for job_id in ids:
            cursor = await conn.execute(
                """
                UPDATE jobs SET status = 'running', attempts = attempts + 1, updated_at = ?
                WHERE id = ? AND status = 'pending'
                RETURNING *
                """,
                (to_iso(now), job_id),
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row is not None:
                claimed.append(_job(row))
        await conn.commit()
        return claimed

    async def finish(
        self,
        job_id: int,
        status: str,
        now: datetime,
        *,
        error: str | None = None,
        trigger_message_id: str | None = None,
    ) -> None:
        await self._db.conn.execute(
            """
            UPDATE jobs SET status = ?, error = ?, updated_at = ?,
                            trigger_message_id = COALESCE(?, trigger_message_id)
            WHERE id = ?
            """,
            (status, error, to_iso(now), trigger_message_id, job_id),
        )
        await self._db.conn.commit()

    async def set_suggestion(self, job_id: int, suggestion_id: str, trigger_message_id: str) -> None:
        await self._db.conn.execute(
            "UPDATE jobs SET suggestion_id = ?, trigger_message_id = ? WHERE id = ?",
            (suggestion_id, trigger_message_id, job_id),
        )
        await self._db.conn.commit()

    async def retry_later(self, job_id: int, run_at: datetime, error: str, now: datetime) -> bool:
        """Возвращает задачу в очередь. False — у диалога уже есть новая задача, эта больше не нужна."""
        try:
            await self._db.conn.execute(
                "UPDATE jobs SET status = 'pending', run_at = ?, error = ?, updated_at = ? WHERE id = ?",
                (to_iso(run_at), error, to_iso(now), job_id),
            )
        except sqlite3.IntegrityError:
            await self.finish(job_id, JOB_STALE, now, error=f"{error} (заменена новой задачей)")
            return False
        await self._db.conn.commit()
        return True

    async def recover_running(self, now: datetime) -> int:
        """После перезапуска: прерванные задачи снова в очередь (или stale, если есть новая)."""
        cursor = await self._db.conn.execute("SELECT id FROM jobs WHERE status = 'running'")
        ids = [row["id"] for row in await cursor.fetchall()]
        await cursor.close()
        for job_id in ids:
            await self.retry_later(job_id, now, "прервана перезапуском сервиса", now)
        return len(ids)

    async def get(self, job_id: int) -> Job | None:
        cursor = await self._db.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
        row = await cursor.fetchone()
        await cursor.close()
        return _job(row) if row else None

    async def counts(self) -> dict[str, int]:
        cursor = await self._db.conn.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")
        rows = await cursor.fetchall()
        await cursor.close()
        return {row["status"]: row["n"] for row in rows}


async def cleanup(db: Database, cutoff: datetime) -> dict[str, int]:
    """Удаляет переписку, подсказки и завершённые задачи старше cutoff."""
    conn = db.conn
    stamp = to_iso(cutoff)
    deleted: dict[str, int] = {}
    statements = {
        "messages": "DELETE FROM messages WHERE received_at < ?",
        "suggestions": "DELETE FROM suggestions WHERE created_at < ?",
        "jobs": "DELETE FROM jobs WHERE updated_at < ? AND status NOT IN ('pending', 'running')",
        "dialogs": """
            DELETE FROM dialogs WHERE updated_at < ?
              AND NOT EXISTS (SELECT 1 FROM messages m WHERE m.dialog_id = dialogs.id)
              AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.dialog_id = dialogs.id)
        """,
    }
    for name, sql in statements.items():
        cursor = await conn.execute(sql, (stamp,))
        deleted[name] = cursor.rowcount
        await cursor.close()
    await conn.commit()
    return deleted
