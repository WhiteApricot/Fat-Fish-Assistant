"""Collect authorized chat text before classification, isolated by sender and session."""
import asyncio
from dataclasses import dataclass


@dataclass
class _PendingBatch:
    texts: list[str]
    future: asyncio.Future[str]
    deadline: float
    timer: asyncio.TimerHandle | None = None


class TextBatch:
    def __init__(self, window=4, maximum=12):
        self.window = window
        self.maximum = maximum
        self.pending = {}

    async def collect(self, key, text):
        loop = asyncio.get_running_loop()
        batch = self.pending.get(key)
        if batch and (len(batch.texts) >= 16 or sum(map(len, batch.texts)) + len(text) > 8000):
            self.flush(key)
            batch = None
        if batch:
            batch.texts.append(text)
            batch.timer.cancel()
            batch.timer = loop.call_at(min(loop.time() + self.window, batch.deadline), self.flush, key)
            return None
        batch = _PendingBatch([text], loop.create_future(), loop.time() + self.maximum)
        batch.timer = loop.call_at(min(loop.time() + self.window, batch.deadline), self.flush, key)
        self.pending[key] = batch
        try:
            return await batch.future
        finally:
            if self.pending.get(key) is batch:
                self.pending.pop(key)
                batch.timer.cancel()

    def flush(self, key):
        batch = self.pending.pop(key, None)
        if batch:
            batch.timer.cancel()
            if not batch.future.done():
                batch.future.set_result("\n".join(batch.texts))
