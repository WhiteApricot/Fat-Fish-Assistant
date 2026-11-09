"""Use the active AstrBot conversation for web reply phrasing and progress."""

import asyncio
import logging
import random
import time

from fat_fish.adapters.qq_reply_delay import WEB_PROGRESS_TYPE, reply_window, wait_before_reply
from fat_fish.integrations.web_search import clean_reply
from fat_fish.memory.experience import contains_secret

_log = logging.getLogger(__name__)
PROGRESS_JITTER = 0.5


def start_web_progress(event, proposed):
    if not event.get_extra('_ffa_reply_delay_bound', False) or event.get_extra('_ffa_web_progress_task'):
        return
    text = _progress_text(proposed)
    if not text:
        return
    latest, media_only = event.get_extra('_ffa_reply_window', (time.monotonic(), False))
    _, maximum = reply_window(command=event.get_extra('_ffa_reply_is_command', False), media=media_only)
    deadline = max(time.monotonic(), latest + maximum) + random.uniform(0, PROGRESS_JITTER)
    task = asyncio.create_task(_send_web_progress(event, deadline, text))
    event.set_extra('_ffa_web_progress_task', task)


def _progress_text(text):
    if not isinstance(text, str):
        return ''
    text = ' '.join(clean_reply(text).splitlines()).strip()
    return text if len(text) <= 80 and '://' not in text and not contains_secret(text) else ''


async def _send_web_progress(event, deadline, text):
    try:
        from astrbot.api.event import MessageChain

        interrupted = event.get_extra('_ffa_reply_interrupted')
        await wait_before_reply(deadline, interrupted)
        if (event.is_stopped() or event.get_extra('_ffa_web_progress_closed', False)
                or event.get_extra('_ffa_reply_superseded', False)):
            return
        await event.send(MessageChain(type=WEB_PROGRESS_TYPE).message(text))
    except Exception as exc:
        _log.info('Web progress unavailable: %s', type(exc).__name__)


async def finish_web_progress(event):
    task = event.get_extra('_ffa_web_progress_task')
    if task is None:
        return
    event.set_extra('_ffa_web_progress_closed', True)
    if not task.done():
        if not event.get_extra('_ffa_web_progress_sending', False):
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
