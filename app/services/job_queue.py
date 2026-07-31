"""Background execution queue.

``POST /execute`` must return before any money moves, so the request only
records a job and hands its id here; a worker picks it up and does the spending.

The queue deliberately stays small. It is an in-process ``asyncio.Queue`` over
the ``jobs`` table, and the table — not the queue — is the source of truth: a
worker claims the job in SQLite before running it, so a duplicate enqueue, a
restart-recovery pass or a second worker can never execute the same job twice.
That claim is also what makes swapping this for arq/Celery a wiring change
rather than a redesign: the replacement only needs to deliver ``job_id`` at
least once.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

JobRunner = Callable[[str], Awaitable[object]]


class JobQueue:
    def __init__(self, runner: JobRunner, *, concurrency: int = 2) -> None:
        self._runner = runner
        self._concurrency = max(1, concurrency)
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._workers: list[asyncio.Task[None]] = []

    @property
    def depth(self) -> int:
        """Jobs waiting for a free worker."""
        return self._queue.qsize()

    @property
    def concurrency(self) -> int:
        return self._concurrency

    @property
    def is_running(self) -> bool:
        return bool(self._workers)

    async def start(self) -> None:
        if self._workers:
            return
        self._workers = [
            asyncio.create_task(self._worker(i), name=f"job-worker-{i}")
            for i in range(self._concurrency)
        ]
        logger.info("job_queue_started", extra={"workers": self._concurrency})

    async def enqueue(self, job_id: str) -> None:
        await self._queue.put(job_id)
        logger.info("job_enqueued", extra={"job_id": job_id, "queue_depth": self.depth})

    async def drain(self) -> None:
        """Wait until everything currently queued has been processed."""
        await self._queue.join()

    async def stop(self) -> None:
        """Stop accepting work and cancel the workers.

        Jobs interrupted here stay ``running`` in the database and are picked up
        again by startup recovery; the step ledger guarantees they are resumed,
        not re-charged.
        """
        workers, self._workers = self._workers, []
        for worker in workers:
            worker.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
            logger.info("job_queue_stopped", extra={"pending": self.depth})

    async def _worker(self, index: int) -> None:
        while True:
            job_id = await self._queue.get()
            try:
                await self._runner(job_id)
            except asyncio.CancelledError:
                raise
            except Exception:  # a broken job must not take the worker down with it
                logger.exception("job_worker_failed", extra={"job_id": job_id, "worker": index})
            finally:
                self._queue.task_done()
