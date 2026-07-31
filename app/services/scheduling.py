"""A background loop that survives its own failures.

Both housekeeping jobs — the reconciler and retention — need the same shape: run
every N seconds, never die because one pass raised, stop cleanly on shutdown.
Having that in one place means a fix to the loop is a fix for both.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)

Pass = Callable[[], Awaitable[Any]]


class PeriodicTask:
    def __init__(self, name: str, *, interval_seconds: float, run: Pass) -> None:
        self.name = name
        self.interval_seconds = interval_seconds
        self._run = run
        self._task: asyncio.Task[None] | None = None

    @property
    def is_running(self) -> bool:
        return self._task is not None

    async def start(self) -> None:
        if self.interval_seconds <= 0:
            logger.info("periodic_disabled", extra={"task": self.name})
            return
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name=self.name)
            logger.info(
                "periodic_started", extra={"task": self.name, "interval": self.interval_seconds}
            )

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.interval_seconds)
            try:
                await self._run()
            except asyncio.CancelledError:
                raise
            except Exception:  # one bad pass must not end the loop
                logger.exception("periodic_pass_failed", extra={"task": self.name})
