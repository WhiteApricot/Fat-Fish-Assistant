"""Asynchronous pacing for FFA QQ sends; no protocol or permission decisions."""
import asyncio
import random
import time

MIN_SECONDS = 3.0
MAX_SECONDS = 5.0
COMMAND_MIN_SECONDS = 2.0
COMMAND_MAX_SECONDS = 3.0


def new_reply_deadline(*, command=False, started=None):
    started = time.monotonic() if started is None else started
    low, high = (COMMAND_MIN_SECONDS, COMMAND_MAX_SECONDS) if command else (MIN_SECONDS, MAX_SECONDS)
    return started + random.uniform(low, high)


async def wait_before_reply(deadline):
    remaining = deadline - time.monotonic()
    if remaining > 0:
        await asyncio.sleep(remaining)


def bind_event_delay(event, prepare_recheck):
    if event.get_platform_name() != 'aiocqhttp' or event.get_extra('_ffa_reply_delay_bound', False):
        return
    original_send = event.send
    started = time.monotonic()
    deadline = new_reply_deadline(started=started)
    command_deadline = None

    async def delayed_send(message):
        nonlocal command_deadline
        still_allowed = prepare_recheck()
        is_command = event.get_extra('_ffa_reply_is_command', False)
        if is_command and command_deadline is None:
            command_deadline = new_reply_deadline(command=True, started=started)
        await wait_before_reply(command_deadline if is_command else deadline)
        if not still_allowed():
            event.stop_event()
            return
        return await original_send(message)

    event.send = delayed_send
    event.set_extra('_ffa_reply_delay_bound', True)
