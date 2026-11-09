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
PART_MIN_SECONDS = 0.5
PART_MAX_SECONDS = 1.0
WEB_PROGRESS_TYPE = 'ffa_web_progress'


def reply_window(*, command=False, media=False):
    if command:
        return COMMAND_MIN_SECONDS, COMMAND_MAX_SECONDS
    if media:
        return MEDIA_MIN_SECONDS, MEDIA_MAX_SECONDS
    return MIN_SECONDS, MAX_SECONDS


def new_reply_deadline(*, command=False, media=False, started=None):
    started = time.monotonic() if started is None else started
    low, high = reply_window(command=command, media=media)
    return started + random.uniform(low, high)


async def wait_before_reply(deadline, interrupted=None):
    remaining = deadline - time.monotonic()
    if remaining > 0:
        if interrupted is None:
            await asyncio.sleep(remaining)
        else:
            try:
                async with asyncio.timeout(remaining):
                    await interrupted.wait()
            except TimeoutError:
                pass


def stop_superseded_runner(event):
    from astrbot.core.pipeline.process_stage.follow_up import _ACTIVE_AGENT_RUNNERS

    runner = _ACTIVE_AGENT_RUNNERS.get(event.unified_msg_origin)
    owner = getattr(getattr(getattr(runner, 'run_context', None), 'context', None), 'event', None)
    if owner is event:
        runner.request_stop()


def supersede_reply(event, task):
    """Wake the old send gate and let AstrBot unwind its session generator."""
    if not event.get_extra('_ffa_reply_delay_bound', False):
        event.stop_event()
        task.cancel()
        return
    event.set_extra('_ffa_reply_superseded', True)
    interrupted = event.get_extra('_ffa_reply_interrupted')
    if interrupted is not None:
        interrupted.set()
    stop_superseded_runner(event)


def bind_event_delay(event, prepare_recheck):
    if event.get_platform_name() != 'aiocqhttp' or event.get_extra('_ffa_reply_delay_bound', False):
        return
    original_send = event.send
    started = time.monotonic()
    event.set_extra('_ffa_reply_started', started)
    # The collector replaces this with the latest admitted message's timestamp.
    event.set_extra('_ffa_reply_window', (started, False))
    interrupted = asyncio.Event()
    event.set_extra('_ffa_reply_interrupted', interrupted)
    sent = False

    async def delayed_send(message):
        nonlocal sent
        if interrupted.is_set():
            return
        still_allowed = prepare_recheck()
        if getattr(message, 'type', None) == WEB_PROGRESS_TYPE:
            if event.get_extra('_ffa_web_progress_closed', False) or not still_allowed():
                return
            event.set_extra('_ffa_web_progress_sending', True)
            try:
                return await original_send(message)
            finally:
                event.set_extra('_ffa_web_progress_sending', False)
        if sent:
            deadline = time.monotonic() + random.uniform(PART_MIN_SECONDS, PART_MAX_SECONDS)
        else:
            latest, media_only = event.get_extra('_ffa_reply_window', (started, False))
            deadline = new_reply_deadline(command=event.get_extra('_ffa_reply_is_command', False),
                                          media=media_only, started=latest)
        await wait_before_reply(deadline, interrupted)
        if interrupted.is_set():
            return
        if not still_allowed():
            event.stop_event()
            return
        event.set_extra('_ffa_reply_committing', True)
        try:
            result = await original_send(message)
            sent = True
            return result
        finally:
            event.set_extra('_ffa_reply_committing', False)

    event.send = delayed_send
    event.set_extra('_ffa_reply_delay_bound', True)
