"""Use the active AstrBot conversation for web reply phrasing and progress."""

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
import logging
import random
import time

from fat_fish.gateway.reply_schedule import WEB_PROGRESS_TYPE
from fat_fish.integrations.web_search import clean_reply
from fat_fish.memory.experience import contains_secret

_log = logging.getLogger(__name__)
PROGRESS_JITTER = 0.5
PROGRESS_SECONDS = 6
_timeouts = ContextVar("ffa_web_timeouts", default=None)


@contextmanager
def observe_search_timeouts():
    observed = []
    token = _timeouts.set(observed)
    try:
        yield observed
    finally:
        _timeouts.reset(token)


def note_search_timeout():
    observed = _timeouts.get()
    if observed is not None:
        observed.append(True)


async def notify_search_timeout(event, *, continuing):
    from astrbot.api.event import MessageChain
    from fat_fish.persona.private_persona import apply_affix

    key = "_ffa_search_timeout_continuing" if continuing else "_ffa_search_timeout_stopped"
    allowed = event.get_extra("_ffa_platform_authorize")
    if not allowed or not await asyncio.to_thread(allowed) or event.get_extra(key):
        return
    event.set_extra(key, True)
    await finish_web_progress(event)
    text = ("有一项资料等得太久，我会继续核对已经拿到的内容。" if continuing
            else "这次查资料等得太久，暂时没能完成核对。")
    try:
        await event.send(MessageChain(type=WEB_PROGRESS_TYPE).message(
            apply_affix(text, event.get_extra("_ffa_persona_notes", ""))))
    except (OSError, RuntimeError) as exc:
        _log.info("Web timeout notice unavailable: %s", type(exc).__name__)


def start_web_progress(event, proposed):
    managed = event.get_extra('_ffa_platform_admission') is not None
    if not managed or event.get_extra('_ffa_web_progress_task'):
        return
    text = _progress_text(proposed)
    if not text:
        return
    latest = event.get_extra('_ffa_latest_arrival', time.monotonic())
    deadline = max(time.monotonic(), latest + PROGRESS_SECONDS) + random.uniform(0, PROGRESS_JITTER)
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

        await asyncio.sleep(max(0, deadline - time.monotonic()))
        allowed = event.get_extra('_ffa_platform_authorize')
        if (event.is_stopped() or event.get_extra('_ffa_web_progress_closed', False)
                or event.get_extra('_ffa_platform_admission') is not None and (not allowed or not allowed())
                or event.get_extra('_ffa_reply_superseded', False)):
            return
        event.set_extra('_ffa_web_progress_sending', True)
        await event.send(MessageChain(type=WEB_PROGRESS_TYPE).message(text))
    except Exception as exc:
        _log.info('Web progress unavailable: %s', type(exc).__name__)
    finally:
        event.set_extra('_ffa_web_progress_sending', False)


async def finish_web_progress(event):
    task = event.get_extra('_ffa_web_progress_task')
    if task is None:
        return
    event.set_extra('_ffa_web_progress_closed', True)
    if not task.done():
        if not event.get_extra('_ffa_web_progress_sending', False):
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
