"""Housekeeping for the two tables that would otherwise grow forever.

``api_idempotency`` stores a full response per key so a lost connection can be
retried; ``media_uploads`` remembers links that stop working after their TTL.
Both are useful for hours and dead weight afterwards.

The rules are deliberately different, because the risk is:

* an idempotency key is dropped only once it is **older than its TTL** — deleting
  one early turns a client retry back into a second real request;
* an upload record is dropped only once the link is **already dead**, so a plan
  can still be warned about a link that is merely close to expiry.

Nothing here touches plans, jobs or the step ledger: those are the money trail
and are kept indefinitely.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.repositories.idempotency import IdempotencyRepository
from app.repositories.media import MediaRepository
from app.services.scheduling import PeriodicTask

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PurgeResult:
    idempotency_keys: int = 0
    expired_uploads: int = 0

    @property
    def total(self) -> int:
        return self.idempotency_keys + self.expired_uploads


class RetentionService:
    def __init__(
        self,
        *,
        idempotency: IdempotencyRepository,
        media: MediaRepository,
        interval_seconds: float = 3600.0,
        idempotency_ttl_hours: float = 24.0,
    ) -> None:
        self.idempotency = idempotency
        self.media = media
        self.interval_seconds = interval_seconds
        self.idempotency_ttl_hours = idempotency_ttl_hours
        self._loop_task: PeriodicTask | None = None

    @property
    def is_running(self) -> bool:
        return self._loop_task is not None and self._loop_task.is_running

    async def start(self) -> None:
        self._loop_task = PeriodicTask(
            "retention", interval_seconds=self.interval_seconds, run=self._purge_and_log
        )
        await self._loop_task.start()

    async def stop(self) -> None:
        if self._loop_task is not None:
            await self._loop_task.stop()
            self._loop_task = None

    async def purge(self, *, now: datetime | None = None) -> PurgeResult:
        now = now or datetime.now(UTC)
        cutoff = now - timedelta(hours=self.idempotency_ttl_hours)
        return PurgeResult(
            idempotency_keys=await self.idempotency.purge_older_than(cutoff.isoformat()),
            expired_uploads=await self.media.purge_expired(now=now),
        )

    async def _purge_and_log(self) -> None:
        result = await self.purge()
        if result.total:
            logger.info(
                "retention_purge",
                extra={
                    "idempotency_keys": result.idempotency_keys,
                    "expired_uploads": result.expired_uploads,
                },
            )
