"""What we uploaded and until when its URL works."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from app.repositories.db import Database


class UploadRecord:
    __slots__ = ("expires_at", "filename", "kind", "size_bytes", "uploaded_at", "url")

    def __init__(self, row: Any) -> None:
        self.url: str = row["url"]
        self.filename: str = row["filename"]
        self.kind: str = row["kind"]
        self.size_bytes: int = row["size_bytes"]
        self.uploaded_at: datetime = datetime.fromisoformat(row["uploaded_at"])
        self.expires_at: datetime = datetime.fromisoformat(row["expires_at"])

    def expires_in_days(self, *, now: datetime | None = None) -> float:
        delta = self.expires_at - (now or datetime.now(UTC))
        return round(delta.total_seconds() / 86_400, 2)

    def is_expired(self, *, now: datetime | None = None) -> bool:
        return self.expires_at <= (now or datetime.now(UTC))


class MediaRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def record(
        self, *, url: str, filename: str, kind: str, size_bytes: int, ttl_days: int
    ) -> datetime:
        now = datetime.now(UTC)
        expires_at = now + timedelta(days=ttl_days)
        await self.db.execute(
            """
            INSERT INTO media_uploads (url, filename, kind, size_bytes, uploaded_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(url) DO UPDATE SET
                uploaded_at = excluded.uploaded_at,
                expires_at  = excluded.expires_at
            """,
            (url, filename, kind, size_bytes, now.isoformat(), expires_at.isoformat()),
        )
        return expires_at

    async def get(self, url: str) -> UploadRecord | None:
        row = await self.db.fetch_one("SELECT * FROM media_uploads WHERE url = ?", (url,))
        return UploadRecord(row) if row else None

    async def find_many(self, urls: list[str]) -> dict[str, UploadRecord]:
        """Only the URLs we uploaded ourselves; anything else is unknown to us."""
        if not urls:
            return {}
        placeholders = ", ".join("?" * len(urls))
        rows = await self.db.fetch_all(
            f"SELECT * FROM media_uploads WHERE url IN ({placeholders})",  # noqa: S608
            tuple(urls),
        )
        return {row["url"]: UploadRecord(row) for row in rows}

    async def purge_expired(self, *, now: datetime | None = None) -> int:
        """Forget links that no longer work — the file is gone upstream anyway."""
        cutoff = (now or datetime.now(UTC)).isoformat()
        return await self.db.execute(
            "DELETE FROM media_uploads WHERE expires_at <= ?", (cutoff,)
        )

    async def count(self) -> int:
        row = await self.db.fetch_one("SELECT COUNT(*) AS n FROM media_uploads")
        return row["n"] if row else 0
