"""SQLite: подключение и схема. Таблицы этапа 2 (dialogs, messages, jobs) добавятся здесь же."""

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
"""


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
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
