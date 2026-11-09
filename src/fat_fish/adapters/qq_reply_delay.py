"""Asynchronous pacing for FFA QQ sends; no protocol or permission decisions."""
import asyncio
import random
import time

MIN_SECONDS = 4.0
MAX_SECONDS = 6.0
MEDIA_MIN_SECONDS = 8.0
MEDIA_MAX_SECONDS = 10.0
COMMAND_MIN_SECONDS = 2.0
COMMAND_MAX_SECONDS = 3.0


def new_reply_deadline(*, command=False, media=False, started=None):
    started = time.monotonic() if started is None else started
    low, high = (MIN_SECONDS, MAX_SECONDS)
    if command:
        low, high = COMMAND_MIN_SECONDS, COMMAND_MAX_SECONDS
    elif media:
        low, high = MEDIA_MIN_SECONDS, MEDIA_MAX_SECONDS
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
    event.set_extra('_ffa_reply_started', started)
    # The collector replaces this with the latest admitted message's timestamp.
    event.set_extra('_ffa_reply_window', (started, False))
    selected = None
    deadline = None

    async def delayed_send(message):
        nonlocal selected, deadline
        still_allowed = prepare_recheck()
        is_command = event.get_extra('_ffa_reply_is_command', False)
        latest, media_only = event.get_extra('_ffa_reply_window', (started, False))
        window = (latest, media_only, is_command)
        if window != selected:
            deadline = new_reply_deadline(command=is_command, media=media_only, started=latest)
            selected = window
        await wait_before_reply(deadline)
        if not still_allowed():
            event.stop_event()
            return
        return await original_send(message)

    event.send = delayed_send
    event.set_extra('_ffa_reply_delay_bound', True)
