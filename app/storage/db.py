"""SQLite: подключение, схема, формат времени."""

from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS suggestions (
    id                 TEXT PRIMARY KEY,
    dialog_id          INTEGER NULL,
    lead_id            INTEGER NULL,
    trigger_message_id TEXT NULL,
    mode               TEXT NOT NULL,
    request_json       TEXT NOT NULL,
    payload_json       TEXT NOT NULL,
    meta_json          TEXT NOT NULL,
    note_id            INTEGER NULL,
    status             TEXT NOT NULL DEFAULT 'new',
    final_text         TEXT NULL,
    upsell_offered     INTEGER NULL,
    created_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_suggestions_lead ON suggestions (lead_id, created_at);

-- Диалог = чат amoCRM. chat_key — id чата, а если его нет, «talk-…» или «contact-…».
CREATE TABLE IF NOT EXISTS dialogs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_key     TEXT NOT NULL UNIQUE,
    talk_id      TEXT NULL,
    contact_id   INTEGER NULL,
    element_type INTEGER NULL,
    element_id   INTEGER NULL,
    origin       TEXT NULL,
    contact_name TEXT NULL,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

-- История переписки из вебхуков. amo_id — ключ идемпотентности: повторный вебхук не создаёт дубль.
CREATE TABLE IF NOT EXISTS messages (
    amo_id          TEXT PRIMARY KEY,
    dialog_id       INTEGER NOT NULL REFERENCES dialogs (id),
    direction       TEXT NOT NULL,
    author_type     TEXT NOT NULL,
    author_name     TEXT NULL,
    text            TEXT NOT NULL,
    attachment_type TEXT NULL,
    created_at      TEXT NOT NULL,
    received_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_messages_dialog ON messages (dialog_id, created_at);

-- Очередь генераций. На диалог не больше одной задачи в статусе pending.
CREATE TABLE IF NOT EXISTS jobs (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    dialog_id          INTEGER NOT NULL REFERENCES dialogs (id),
    trigger_message_id TEXT NOT NULL,
    run_at             TEXT NOT NULL,
    status             TEXT NOT NULL,
    attempts           INTEGER NOT NULL DEFAULT 0,
    error              TEXT NULL,
    suggestion_id      TEXT NULL,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_jobs_pending ON jobs (dialog_id) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS ix_jobs_due ON jobs (status, run_at);
"""


def to_iso(moment: datetime) -> str:
    """Единый формат времени в БД: UTC, миллисекунды. Строки такого вида сравниваются как время."""
    return moment.astimezone(UTC).isoformat(timespec="milliseconds")


def from_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def utcnow() -> datetime:
    return datetime.now(UTC)


class Database:
    def __init__(self, path: Path):
        self.path = path
        self._conn: aiosqlite.Connection | None = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("База данных не подключена")
        return self._conn

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()

    async def commit(self) -> None:
        await self.conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
