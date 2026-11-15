"""Opt-in, content-free Lark timings measured on the existing request path."""
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
import logging
import re
import time

logger = logging.getLogger(__name__)
_GREETING = re.compile(r"(?:你好(?:呀|啊|哦)?|您好|嗨|哈喽|早上好|晚上好|晚安|谢谢(?:你|啦)?|hello|hi)[。！!，,.～~\s]*", re.I)
_INTERVALS = {
    "native_ms": ("T1", "T2"), "authority_ms": ("T2", "T3"),
    "batch_ms": ("T3", "T4"), "intent_ms": ("T4", "T5"),
    "memory_ms": ("memory_start", "memory_end"), "media_ms": ("media_start", "media_end"),
    "pace_ms": ("pace_start", "pace_end"), "model_first_output_ms": ("T6", "T7"),
    "submit_ms": ("T8", "T9"), "gateway_ms": ("T2", "T9"),
}


def is_greeting(text):
    return bool(_GREETING.fullmatch(text.strip()))


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

    def mark(self, step):
        if step not in self.times:
            self.times[step] = time.monotonic()

    def log(self):
        fields = {"id": self.ident, "platform": "lark", "kind": self.kind,
                  "category": self.category, "status": self.outcome or "no_submission"}
        if self.error:
            fields["error"] = self.error
        fields.update({name: round((self.times[end] - self.times[start]) * 1000, 1)
                       for name, (start, end) in _INTERVALS.items() if start in self.times and end in self.times})
        if self.server is not None:
            if "T9" in self.times:
                elapsed = self.times["T9"] - self.times["T1"]
                fields["server_to_result_ms"] = round((self.arrival_wall - self.server + elapsed) * 1000, 1)
            fields["server_to_receive_ms"] = round((self.arrival_wall - self.server) * 1000, 1)
        logger.info("FFA Lark latency %s", json.dumps(fields, separators=(",", ":")))


def trace(event):
    return event.get_extra("_ffa_lark_latency", None)


@contextmanager
def submitted(event, *, streaming=False):
    timeline = trace(event)
    if timeline is None or "T8" in timeline.times:
        yield
        return
    if streaming:
        timeline.category = "G"
    timeline.mark("T8")
    try:
        yield
    except BaseException as exc:
        timeline.outcome, timeline.error = "failed", type(exc).__name__
        raise
    else:
        timeline.outcome = "submitted"
    finally:
        timeline.mark("T9")


def attach_native_ingress(adapter, private_dir):
    """Observe the pinned adapter's callback without replacing its event receiver."""
    if not all(callable(getattr(adapter, name, None)) for name in (
        "convert_msg", "create_event", "_parse_message_components",
    )):
        return lambda: None
    convert, create, parse = adapter.convert_msg, adapter.create_event, adapter._parse_message_components
    received = ContextVar("ffa_lark_native_receive", default=None)

    async def timed_convert(event):
        clock = ((time.time(), time.monotonic()) if private_dir is not None
                 and (private_dir / "ffa-lark-latency").is_file() else None)
        token = received.set(clock)
        try:
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
        for name, hook in hooks.items():
            if getattr(adapter, name, None) is hook:
                if name in previous:
                    setattr(adapter, name, previous[name])
                else:
                    delattr(adapter, name)

    return detach
