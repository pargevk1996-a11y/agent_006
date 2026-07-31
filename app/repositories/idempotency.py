"""Storage for ``Idempotency-Key`` replay protection on our own API."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from app.repositories.db import Database, dumps


class IdempotencyRecord:
    __slots__ = ("endpoint", "headers", "key", "request_hash", "response_body", "status_code")

    def __init__(self, row: Any) -> None:
        self.key: str = row["key"]
        self.endpoint: str = row["endpoint"]
        self.request_hash: str = row["request_hash"]
        self.status_code: int | None = row["status_code"]
        self.headers: dict[str, str] = json.loads(row["headers"]) if row["headers"] else {}
        self.response_body: Any = (
            json.loads(row["response_body"]) if row["response_body"] is not None else None
        )

    @property
    def is_complete(self) -> bool:
        return self.response_body is not None


class IdempotencyRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def claim(
        self, *, key: str, endpoint: str, request_hash: str
    ) -> IdempotencyRecord | None:
        """Reserve a key. ``None`` means we own it; a record means it already existed.

        ``INSERT OR IGNORE`` against the primary key is the whole race protection:
        two simultaneous retries of the same request cannot both get ``None``.
        """
        inserted = await self.db.execute(
            """
            INSERT OR IGNORE INTO api_idempotency
                (key, endpoint, request_hash, status_code, headers, response_body, created_at)
            VALUES (?, ?, ?, NULL, NULL, NULL, ?)
            """,
            (key, endpoint, request_hash, datetime.now(UTC).isoformat()),
        )
        return None if inserted == 1 else await self.get(key)

    async def get(self, key: str) -> IdempotencyRecord | None:
        row = await self.db.fetch_one("SELECT * FROM api_idempotency WHERE key = ?", (key,))
        return IdempotencyRecord(row) if row else None

    async def complete(
        self, key: str, *, status_code: int, headers: dict[str, str], body: Any
    ) -> None:
        await self.db.execute(
            """
            UPDATE api_idempotency
               SET status_code = ?, headers = ?, response_body = ?, completed_at = ?
             WHERE key = ?
            """,
            (status_code, dumps(headers), dumps(body), datetime.now(UTC).isoformat(), key),
        )

    async def purge_older_than(self, created_before: str) -> int:
        """Retention. Only fully answered keys are dropped, and only past their TTL.

        Deleting a key early would turn a client's retry back into a second real
        request — the exact thing the header exists to prevent.
        """
        return await self.db.execute(
            "DELETE FROM api_idempotency WHERE created_at < ? AND response_body IS NOT NULL",
            (created_before,),
        )

    async def count(self) -> int:
        row = await self.db.fetch_one("SELECT COUNT(*) AS n FROM api_idempotency")
        return row["n"] if row else 0

    async def release(self, key: str) -> None:
        """Drop an unfinished claim so a failed attempt can be retried.

        Guarded by ``response_body IS NULL``: a stored answer is never deleted.
        """
        await self.db.execute(
            "DELETE FROM api_idempotency WHERE key = ? AND response_body IS NULL", (key,)
        )
