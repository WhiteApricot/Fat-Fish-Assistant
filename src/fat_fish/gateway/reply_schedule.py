"""One reply deadline for a managed turn, measured from the latest activity."""
import asyncio
import random


MIN_REPLY_SECONDS = 4.0
MAX_REPLY_SECONDS = 6.5
WEB_PROGRESS_TYPE = "ffa_web_progress"


class ReplySchedule:
    def __init__(self, arrived, *, sample=random.uniform):
        self.delay = sample(MIN_REPLY_SECONDS, MAX_REPLY_SECONDS)
        self.update(arrived)

    def update(self, arrived, *, latest=None):
        deadline = arrived + self.delay
        self.deadline = min(deadline, latest) if latest is not None else deadline

    async def wait(self):
        remaining = self.deadline - asyncio.get_running_loop().time()
        if remaining > 0:
            await asyncio.sleep(remaining)
