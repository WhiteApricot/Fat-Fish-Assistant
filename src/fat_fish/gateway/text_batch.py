"""Collect authorized chat text before the Agent, isolated by sender and session."""
import asyncio
from dataclasses import dataclass
from typing import Callable


@dataclass
class _PendingBatch:
    texts: list[str]
    future: asyncio.Future[str]
    deadline: float
    timer: asyncio.TimerHandle | None = None
    on_arrival: Callable[[float], None] | None = None


class TextBatch:
    def __init__(self, window=1.2, maximum=12):
        self.window = window
        self.maximum = maximum
        self.pending = {}

    async def collect(self, key, text, *, on_arrival=None):
        loop = asyncio.get_running_loop()
        now = loop.time()
        batch = self.pending.get(key)
        if batch and (len(batch.texts) >= 16 or sum(map(len, batch.texts)) + len(text) > 8000):
            self.flush(key)
            batch = None
        if batch:
            batch.texts.append(text)
            if batch.on_arrival is not None:
                batch.on_arrival(now)
            batch.timer.cancel()
            batch.timer = loop.call_at(min(now + self.window, batch.deadline), self.flush, key)
            return None
        if on_arrival is not None:
            on_arrival(now)
        batch = _PendingBatch([text], loop.create_future(), now + self.maximum, on_arrival=on_arrival)
        batch.timer = loop.call_at(min(now + self.window, batch.deadline), self.flush, key)
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
