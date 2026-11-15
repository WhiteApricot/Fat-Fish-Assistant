"""Opt-in, content-free Lark timings measured on the existing request path."""
from contextlib import contextmanager
from contextvars import ContextVar
import asyncio
import hashlib
import json
import logging
import time

logger = logging.getLogger(__name__)
_INTERVALS = {
    "native_ms": ("T1", "T2"), "authority_ms": ("T2", "T3"),
    "batch_ms": ("T3", "T4"), "gateway_prepare_ms": ("T2", "ready"),
    "pre_agent_ms": ("T2", "T6"), "agent_ms": ("T6", "T7"),
    "agent_completion_ms": ("T6", "agent_finished"),
    "memory_ms": ("memory_start", "memory_end"), "media_ms": ("media_start", "media_end"),
    "pace_ms": ("pace_start", "pace_end"), "model_first_output_ms": ("T6", "T7"),
    "submit_ms": ("T8", "T9"), "gateway_ms": ("T2", "T9"),
    "gateway_to_first_api_result_ms": ("T2", "first_api_result"),
}


class LarkLatency:
    def __init__(self, raw, received):
        try:
            self.server = int(raw.create_time) / 1000
        except (AttributeError, TypeError, ValueError):
            self.server = None
        self.arrival_wall, mono = received
        self.times = {"T1": mono}
        self.ident = hashlib.sha256(str(raw.message_id).encode()).hexdigest()[:16]
        self.kind = "group" if raw.chat_type == "group" else "private"
        self.category = "D"
        self.outcome = None
        self.error = None
        self.tools = 0.0
        self.tokens = None

    def mark(self, step):
        if step not in self.times:
            self.times[step] = time.monotonic()

    def add_tool_time(self, start):
        self.tools += time.monotonic() - start

    def log(self):
        fields = {"id": self.ident, "platform": "lark", "kind": self.kind,
                  "category": self.category, "status": self.outcome or "no_submission"}
        if self.error:
            fields["error"] = self.error
        if self.tools:
            fields["tool_ms"] = round(self.tools * 1000, 1)
        if self.tokens is not None:
            fields["agent_tokens"] = self.tokens
        fields.update({name: round((self.times[end] - self.times[start]) * 1000, 1)
                       for name, (start, end) in _INTERVALS.items() if start in self.times and end in self.times})
        if self.server is not None:
            if "T9" in self.times:
                elapsed = self.times["T9"] - self.times["T1"]
                fields["server_to_result_ms"] = round((self.arrival_wall - self.server + elapsed) * 1000, 1)
            if "first_api_result" in self.times:
                elapsed = self.times["first_api_result"] - self.times["T1"]
                fields["server_to_first_api_result_ms"] = round((self.arrival_wall - self.server + elapsed) * 1000, 1)
            fields["server_to_receive_ms"] = round((self.arrival_wall - self.server) * 1000, 1)
        logger.info("FFA Lark latency %s", json.dumps(fields, separators=(",", ":")))


def trace(event):
    return event.get_extra("_ffa_lark_latency", None)


@contextmanager
def submitted(event, *, streaming=False, progress=False):
    timeline = trace(event)
    if timeline is None:
        yield
        return
    if streaming:
        timeline.category = "G"
    started = time.monotonic()
    try:
        yield
    except BaseException as exc:
        if not progress:
            timeline.outcome, timeline.error = "failed", type(exc).__name__
        raise
    else:
        timeline.mark("first_api_result")
        if not progress:
            timeline.outcome, timeline.error = "submitted", None
    finally:
        if not progress:
            timeline.times["T8"] = started
            timeline.times["T9"] = time.monotonic()


def attach_native_ingress(adapter, private_dir):
    """Observe the pinned adapter's callback without replacing its event receiver."""
    if not all(callable(getattr(adapter, name, None)) for name in (
        "convert_msg", "create_event", "_parse_message_components",
    )):
        return lambda: None
    convert, create, parse = adapter.convert_msg, adapter.create_event, adapter._parse_message_components
    received = ContextVar("ffa_lark_native_receive", default=None)
    profile_tasks = set()

    def defer_profile(envelope):
        from lark_oapi.api.contact.v3 import GetUserRequest
        from astrbot.core.platform.sources.lark.lark_adapter import (
            USER_NAME_CACHE_MAX_SIZE, USER_NAME_CACHE_TTL_SECONDS, USER_NAME_FAILURE_CACHE_TTL_SECONDS,
            USER_NAME_LOOKUP_TIMEOUT_SECONDS,
        )

        message = getattr(envelope, "message", None)
        sender = getattr(envelope, "sender", None)
        subject = getattr(getattr(sender, "sender_id", None), "open_id", None)
        if (getattr(message, "chat_type", None) != "p2p" or getattr(sender, "sender_type", "user") != "user"
                or not isinstance(subject, str) or not subject.startswith("ou_")):
            return
        cache = adapter._user_name_cache
        now = time.time()
        cached = cache.get(subject)
        if cached and cached[1] >= now:
            return
        # The native adapter uses this fallback when Contact is unavailable.
        # Native IM routing and FFA identity still use the authenticated ID.
        fallback = (subject[:8], now + USER_NAME_FAILURE_CACHE_TTL_SECONDS)
        cache[subject] = fallback
        if len(cache) > USER_NAME_CACHE_MAX_SIZE:
            cache.pop(next(iter(cache)))
        if len(profile_tasks) >= 16:
            return

        async def refresh():
            request = GetUserRequest.builder().user_id(subject).user_id_type("open_id").build()
            try:
                async with asyncio.timeout(USER_NAME_LOOKUP_TIMEOUT_SECONDS):
                    response = await adapter.lark_api.contact.v3.user.aget(request)
                if response.success() and response.data and response.data.user:
                    name = str(response.data.user.name or "").strip()
                    if name and cache.get(subject) == fallback:
                        cache[subject] = (name, time.time() + USER_NAME_CACHE_TTL_SECONDS)
            except (AttributeError, OSError, RuntimeError, ValueError, TimeoutError) as exc:
                logger.debug("Lark optional profile unavailable (%s)", type(exc).__name__)

        task = asyncio.create_task(refresh())
        profile_tasks.add(task)
        task.add_done_callback(profile_tasks.discard)

    async def timed_convert(event):
        clock = ((time.time(), time.monotonic()) if private_dir is not None
                 and (private_dir / "ffa-lark-latency").is_file() else None)
        token = received.set(clock)
        try:
            if hasattr(adapter, "_user_name_cache"):
                defer_profile(getattr(event, "event", None))
            return await convert(event)
        finally:
            received.reset(token)

    def timed_create(native):
        event = create(native)
        clock = received.get()
        if clock is not None and native.raw_message is not None:
            event.set_extra("_ffa_lark_latency", LarkLatency(native.raw_message, clock))
        return event

    async def parse_components(*, message_id, message_type, content, at_map):
        if message_type == "merge_forward":
            return list(at_map.values())
        try:
            return await parse(message_id=message_id, message_type=message_type, content=content, at_map=at_map)
        except Exception as exc:
            if message_type != "audio":
                raise
            # Retain the authenticated event so the ordinary permission gate
            # can report the failure. The exception may contain resource URLs.
            logger.debug("Lark native voice preparation failed (%s)", type(exc).__name__)
            return list(at_map.values())

    hooks = {"convert_msg": timed_convert, "create_event": timed_create, "_parse_message_components": parse_components}
    previous = {name: vars(adapter)[name] for name in hooks if name in vars(adapter)}
    for name, hook in hooks.items():
        setattr(adapter, name, hook)

    def detach():
        for task in profile_tasks:
            task.cancel()
        for name, hook in hooks.items():
            if getattr(adapter, name, None) is hook:
                if name in previous:
                    setattr(adapter, name, previous[name])
                else:
                    delattr(adapter, name)

    return detach
