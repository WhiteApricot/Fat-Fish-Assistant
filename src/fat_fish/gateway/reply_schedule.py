"""One reply deadline for a managed turn, measured from the latest activity."""
import asyncio
import random


DEFAULT_REPLY_WINDOW = (4.0, 6.5)
WEB_PROGRESS_TYPE = "ffa_web_progress"


class ReplySchedule:
    def __init__(self, arrived, *, window=DEFAULT_REPLY_WINDOW, sample=random.uniform):
        self.delay = sample(*window)
        self.update(arrived)

    def update(self, arrived, *, latest=None):
        deadline = arrived + self.delay
        self.deadline = min(deadline, latest) if latest is not None else deadline

    async def wait(self):
        remaining = self.deadline - asyncio.get_running_loop().time()
        if remaining > 0:
            await asyncio.sleep(remaining)
