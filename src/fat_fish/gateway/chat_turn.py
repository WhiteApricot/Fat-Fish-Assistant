"""Versioned text turns with a single event-loop submission boundary."""
import asyncio
from dataclasses import dataclass, field

from fat_fish.gateway.reply_schedule import DEFAULT_REPLY_WINDOW, ReplySchedule


class Superseded(asyncio.CancelledError):
    """A newer admitted input owns the response."""


@dataclass(eq=False)
class Input:
    key: tuple
    arrived: float
    message: object
    native: object
    resolved: asyncio.Event = field(default_factory=asyncio.Event)


class Turn:
    def __init__(self, coordinator, item, principal, previous, reply_window):
        self.coordinator = coordinator
        self.key = item.key
        self.principal = principal
        self.native = item.native
        self.task = asyncio.current_task()
        self.superseded = False
        self.committed = False
        self.version = previous.version + 1 if previous else 1
        self.items = sorted([*previous.items, item] if previous else [item], key=lambda part: part.arrived)
        self.limit = self.items[0].arrived + coordinator.maximum
        self.schedule = previous.schedule if previous else ReplySchedule(item.arrived, window=reply_window)
        self.schedule.update(self.items[-1].arrived, latest=self.limit)

    def check(self):
        if self.superseded:
            raise Superseded()

    def pending(self):
        if asyncio.get_running_loop().time() >= self.limit:
            return ()
        return self.coordinator.pending.get(self.key, ())

    async def ready(self):
        self.check()
        while not self.committed and (pending := tuple(self.pending())):
            try:
                async with asyncio.timeout_at(self.limit):
                    await asyncio.gather(*(item.resolved.wait() for item in pending))
            except TimeoutError:
                break
            self.check()

    def commit(self):
        self.check()
        if not self.committed and self.pending():
            return False
        self.committed = True
        return True


class ChatTurns:
    def __init__(self, maximum=12):
        self.maximum = maximum
        self.active = {}
        self.pending = {}

    def receive(self, message, native, arrived):
        key = message.conversation.storage_key, message.sender.storage_key
        item = Input(key, arrived, message, native)
        self.pending.setdefault(key, set()).add(item)
        return item

    def resolve(self, item):
        if item is None or item.resolved.is_set():
            return
        entries = self.pending[item.key]
        entries.remove(item)
        if not entries:
            self.pending.pop(item.key)
        item.resolved.set()

    def admit(self, item, principal, *, reply_window=DEFAULT_REPLY_WINDOW):
        previous = self.active.get(item.key)
        if (previous and (previous.committed or previous.principal != principal
                          or asyncio.get_running_loop().time() >= previous.limit
                          or len(previous.items) >= 16
                          or sum(len(part.message.text) for part in previous.items) + len(item.message.text) > 8000)):
            previous = None
        turn = Turn(self, item, principal, previous, reply_window)
        self.active[item.key] = turn
        if previous:
            previous.superseded = True
            previous.native.set_extra("_ffa_reply_superseded", True)
            previous.native.stop_event()
            previous.task.cancel()
        self.resolve(item)
        return turn

    def finish(self, turn):
        if self.active.get(turn.key) is turn:
            del self.active[turn.key]
