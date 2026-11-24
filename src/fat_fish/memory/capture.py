"""Bounded conversational admission and durable capture for trusted producers."""

import asyncio
from collections import Counter
import logging
import sqlite3
from time import time

from fat_fish.memory.erasure import LedgerError
from fat_fish.memory.records import EventRecord

logger = logging.getLogger(__name__)
CAPTURE_CAPACITY = 64


class CaptureQueue:
    """The RAM queue is for ordinary input only.

    Explicit and verified events use the durable repository path and return an
    identifier only after the SQLite transaction completes. Erasure is never
    put into this queue.
    """

    def __init__(self, repository, *, clock=None):
        self.repository = repository
        self.clock = clock or (lambda: int(time() * 1000))
        self._queue = asyncio.Queue(maxsize=CAPTURE_CAPACITY)
        self._worker = None
        self._counts = Counter()

    def start(self):
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run())

    def offer(self, event: EventRecord, verify) -> bool:
        try:
            self._queue.put_nowait((event, verify))
        except asyncio.QueueFull:
            self._counts["dropped"] += 1
            return False
        self._counts["queued_ram"] += 1
        return True

    def _persist_when_authorized(self, event, verify):
        if not verify():
            raise PermissionError("Memory capture authorization changed")
        return self.repository.record_event(event, self.clock(), authorize=verify)

    async def persist(self, event: EventRecord, verify) -> int:
        try:
            event_id = await asyncio.to_thread(self._persist_when_authorized, event, verify)
        except (OSError, ValueError, PermissionError, LedgerError, sqlite3.Error):
            self._counts["failed"] += 1
            raise
        self._counts["persisted"] += 1
        return event_id

    async def _run(self) -> None:
        while True:
            event, verify = await self._queue.get()
            try:
                await self.persist(event, verify)
            except (OSError, ValueError, PermissionError, LedgerError, sqlite3.Error) as exc:
                logger.info("Ordinary memory capture skipped (%s)", type(exc).__name__)
            finally:
                self._queue.task_done()

    async def stop(self) -> None:
        if self._worker is None:
            return
        self._worker.cancel()
        try:
            await self._worker
        except asyncio.CancelledError:
            pass
        finally:
            self._worker = None
        while not self._queue.empty():
            self._queue.get_nowait()
            self._queue.task_done()
            self._counts["dropped_shutdown"] += 1

    def counts(self) -> dict[str, int]:
        return dict(self._counts)
