"""SQLite storage.

A single shared connection guarded by an ``asyncio.Lock``: SQLite writes are
serialised anyway, and one connection keeps WAL semantics and transactions
predictable. Plans and jobs are stored as their JSON documents plus a few
promoted columns; the ``step_executions`` table is a real ledger — it is what
makes a second ``execute`` call safe.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS plans (
    plan_id              TEXT PRIMARY KEY,
    created_at           TEXT NOT NULL,
    mode                 TEXT NOT NULL,
    status               TEXT NOT NULL,
    budget_rub           REAL NOT NULL,
    total_estimated_rub  REAL NOT NULL,
    job_id               TEXT,
    document             TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    job_id       TEXT PRIMARY KEY,
    plan_id      TEXT NOT NULL,
    status       TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    document     TEXT NOT NULL,
    FOREIGN KEY (plan_id) REFERENCES plans(plan_id)
);
CREATE INDEX IF NOT EXISTS idx_jobs_plan ON jobs(plan_id);

-- One row per (plan, step). The UNIQUE constraint is the guarantee that a step
-- is never launched twice under two different idempotency keys.
CREATE TABLE IF NOT EXISTS step_executions (
    idempotency_key TEXT PRIMARY KEY,
    plan_id         TEXT NOT NULL,
    step_id         TEXT NOT NULL,
    job_id          TEXT NOT NULL,
    request_hash    TEXT NOT NULL,
    generation_id   INTEGER,
    status          TEXT NOT NULL,
    actual_cost_rub REAL NOT NULL DEFAULT 0,
    updated_at      TEXT NOT NULL,
    UNIQUE (plan_id, step_id)
);
CREATE INDEX IF NOT EXISTS idx_step_generation ON step_executions(generation_id);

CREATE TABLE IF NOT EXISTS webhook_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    generation_id INTEGER,
    event         TEXT,
    received_at   TEXT NOT NULL,
    payload       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_webhook_generation ON webhook_events(generation_id);
"""


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        if self._conn is not None:
            return
        if self.path.parent and str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(str(self.path))
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database.connect() must be awaited before use")
        return self._conn

    async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        async with self._lock:
            await self.conn.execute(sql, params)
            await self.conn.commit()

    async def fetch_one(self, sql: str, params: tuple[Any, ...] = ()) -> aiosqlite.Row | None:
        async with self._lock, self.conn.execute(sql, params) as cursor:
            return await cursor.fetchone()

    async def fetch_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[aiosqlite.Row]:
        async with self._lock, self.conn.execute(sql, params) as cursor:
            return list(await cursor.fetchall())


def dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def loads(raw: str) -> Any:
    return json.loads(raw)
