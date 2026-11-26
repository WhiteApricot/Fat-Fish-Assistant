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


class ExtractionWorker:
    """One bounded consumer of the durable extraction queue per runtime."""

    def __init__(self, gateway, provider_for, authorize, *, poll_interval=1.0):
        if poll_interval <= 0:
            raise ValueError("Worker poll interval must be positive")
        self.gateway = gateway
        self.provider_for = provider_for
        self.authorize = authorize
        self.poll_interval = poll_interval
        self._wake = asyncio.Event()
        self.ready = asyncio.Event()
        self._stop = False
        self._task = None

    def start(self):
        if self._task is None or self._task.done():
            self._stop = False
            self.ready.clear()
            self._task = asyncio.create_task(self._run())

    def notify(self):
        self._wake.set()

    async def stop(self):
        self._stop = True
        self.notify()
        if self._task is not None:
            await self._task
            self._task = None

    async def _run(self):
        repository = self.gateway.queue.repository
        while not self._stop:
            self._wake.clear()
            try:
                with repository.keep_wal_open():
                    self.ready.set()
                    await self._consume(repository)
            except (OSError, sqlite3.Error, LedgerError) as exc:
                self.ready.clear()
                logger.warning("FFA extraction worker could not open WAL (%s)", type(exc).__name__)
                try:
                    await asyncio.wait_for(self._wake.wait(), self.poll_interval)
                except TimeoutError:
                    pass

    async def _consume(self, repository):
        while not self._stop:
            self._wake.clear()
            try:
                now = self.gateway.queue.clock()
                due = await asyncio.to_thread(repository.extraction_due, now)
                if due and not self._stop and await self.gateway.extract_next(self.provider_for, self.authorize):
                    continue
            except (OSError, sqlite3.Error, LedgerError) as exc:
                logger.warning("FFA extraction worker will retry (%s)", type(exc).__name__)
            try:
                await asyncio.wait_for(self._wake.wait(), self.poll_interval)
            except TimeoutError:
                pass
