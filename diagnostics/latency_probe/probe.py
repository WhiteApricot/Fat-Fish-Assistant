"""TEMPORARY. Explicitly loaded by the local latency launcher only."""
import asyncio
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import wraps
import inspect
import json
from itertools import count
from queue import Full, Queue
import sqlite3
from threading import Thread
import time
from uuid import uuid4


active = ContextVar("latency_trace", default=None)
parent = ContextVar("latency_operation", default=None)
active_stage = ContextVar("latency_stage", default=None)
socket_arrival = ContextVar("latency_socket_arrival", default=None)
delivery = ContextVar("latency_delivery", default=None)
api = ContextVar("latency_api", default=None)
STAGES = tuple(f"T{number}" for number in range(22))
MARKS = {"T6": "T11", "T7": "T12", "agent_finished": "T15"}


def clock():
    # Python 3.12's Windows monotonic clock has a coarser tick than QPC.
    return time.time_ns(), time.perf_counter_ns()


def utc(wall):
    return datetime.fromtimestamp(wall / 1e9, timezone.utc).isoformat(timespec="milliseconds")


class Writer:
    def __init__(self, path):
        self.queue = Queue(maxsize=8192)
        self.dropped = 0
        self.worker = Thread(target=self.write, args=(path,), daemon=True)
        self.worker.start()

    def emit(self, row):
        try:
            self.queue.put_nowait(row)
        except Full:
            self.dropped += 1

    def write(self, path):
        with path.open("a", encoding="utf-8") as output:
            while (row := self.queue.get()) is not None:
                output.write(json.dumps(row, ensure_ascii=False) + "\n")
                output.flush()

    def close(self):
        if self.worker.is_alive():
            try:
                self.queue.put(None, timeout=2)
            except Full:
                pass
            self.worker.join(timeout=3)
        if self.dropped:
            print(f"Latency probe dropped {self.dropped} records", flush=True)


class Trace:
    def __init__(self, writer, raw):
        self.writer = writer
        self.ident = uuid4().hex
        self.zero = clock()
        self.sequence = count(1)
        self.seen = set()
        self.send_failed = set()
        self.message_type = getattr(raw, "message_type", None)
        if self.message_type not in {"text", "post", "image", "file", "audio", "merge_forward"}:
            self.message_type = "other"
        self.chat_type = "group" if getattr(raw, "chat_type", None) == "group" else "private"
        try:
            self.emit("T1", "server_created", "point", (int(raw.create_time) * 10**6, None))
        except (AttributeError, TypeError, ValueError, OverflowError, OSError):
            pass
        if received := socket_arrival.get():
            self.emit("T2", "websocket_frame_received", "point", received)

    def emit(self, stage, operation, event, when=None, *, ident=None, up=None, status="ok", **fields):
        wall, mono = when or clock()
        self.seen.add(stage)
        self.writer.emit({
            "trace_id": self.ident, "stage": stage, "operation": operation, "event": event,
            "operation_id": ident or next(self.sequence), "parent_operation_id": up,
            "utc": utc(wall), "monotonic_ms": None if mono is None else round((mono - self.zero[1]) / 1e6, 3),
            "status": status, **fields,
        })

    def summary(self, status, tokens):
        self.writer.emit({
            "trace_id": self.ident, "event": "summary", "utc": utc(time.time_ns()), "status": status,
            "message_type": self.message_type, "chat_type": self.chat_type, "tokens": tokens,
            "stages": {stage: "observed" if stage in self.seen else
                       "unobserved" if stage in {"T0", "T1", "T2", "T21"} else "not_applicable"
                       for stage in STAGES},
        })


class Span:
    def __init__(self, trace, stage, name, fields):
        self.status = "ok"
        self.fields = {}
        self.trace, self.stage, self.name = trace, stage, name
        self.ident, self.up = next(trace.sequence), parent.get()
        self.start = clock()
        trace.emit(stage, name, "start", self.start, ident=self.ident, up=self.up, **fields)

    def end(self):
        end = clock()
        self.trace.emit(self.stage, self.name, "end", end, ident=self.ident, up=self.up,
                        status=self.status, duration_ms=round((end[1] - self.start[1]) / 1e6, 3), **self.fields)


@contextmanager
def span(stage, name, **fields):
    trace = active.get()
    if trace is None:
        yield None
        return
    operation = Span(trace, stage, name, fields)
    token = parent.set(operation.ident)
    phase = active_stage.set(stage)
    try:
        yield operation
    except BaseException as exc:
        operation.status = ("cancelled" if isinstance(exc, (asyncio.CancelledError, GeneratorExit)) else
                            "timeout" if isinstance(exc, TimeoutError) else "error")
        operation.fields["error_type"] = type(exc).__name__
        raise
    finally:
        active_stage.reset(phase)
        parent.reset(token)
        operation.end()


def instrument(owner, method, stage, label=None):
    original = getattr(owner, method)
    name = label or method
    if inspect.iscoroutinefunction(original):
        @wraps(original)
        async def wrapped(*args, **kwargs):
            fields = {"sampled_delay_ms": round(args[0].delay * 1000, 3)} if name == "random_reply_wait" else {}
            with span(stage, name, **fields) as operation:
                result = await original(*args, **kwargs)
                if operation and result is None and method == "admit_async":
                    operation.status = "denied_or_duplicate"
                return result
    else:
        @wraps(original)
        def wrapped(*args, **kwargs):
            phase = "T5" if method == "authorize" and active_stage.get() == "T5" else stage
            with span(phase, name) as operation:
                result = original(*args, **kwargs)
                if operation and name == "turn_admission_and_batch":
                    related = [timeline_for(item.native) for item in result.items]
                    operation.fields["source_trace_ids"] = [trace.probe.ident for trace in related
                                                             if getattr(trace, "probe", None) is not None]
                return result
    setattr(owner, method, wrapped)


def timeline_for(event):
    return event.get_extra("_ffa_lark_latency", None)


def install(writer):
    import aiohttp
    import httpx
    from lark_oapi.ws.client import Client
    from lark_oapi.api.im.v1.resource.message import Message
    from lark_oapi.api.cardkit.v1.resource.card import Card
    from lark_oapi.api.cardkit.v1.resource.card_element import CardElement
    from astrbot.core.platform.sources.lark.lark_adapter import LarkPlatformAdapter
    from astrbot.core.platform.sources.lark.lark_event import LarkMessageEvent
    from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial
    from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor
    from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
    from astrbot.core.pipeline import context_utils
    from astrbot.core.star.star_handler import EventType
    from fat_fish.adapters import lark_latency

    handle_frame = Client._handle_message
    @wraps(handle_frame)
    async def frame(self, payload):
        token = socket_arrival.set(clock())
        try:
            return await handle_frame(self, payload)
        finally:
            socket_arrival.reset(token)
    Client._handle_message = frame

    dispatch = context_utils.call_event_hook

    @wraps(dispatch)
    async def tool_event(event, hook, *args, **kwargs):
        if hook is not EventType.OnUsingLLMToolEvent:
            return await dispatch(event, hook, *args, **kwargs)
        with span("T13", "tool_start_hook") as operation:
            stopped = await dispatch(event, hook, *args, **kwargs)
            if operation and stopped:
                operation.status = "stopped"
            return stopped
    context_utils.call_event_hook = tool_event

    convert = LarkPlatformAdapter.convert_msg
    @wraps(convert)
    async def ingress(self, event):
        raw = getattr(getattr(event, "event", None), "message", None)
        if raw is None:
            return await convert(self, event)
        trace = Trace(writer, raw)
        token = active.set(trace)
        try:
            with span("T3", "native_conversion"):
                return await convert(self, event)
        finally:
            if not getattr(trace, "queued", False):
                trace.summary("not_queued", None)
            active.reset(token)
    LarkPlatformAdapter.convert_msg = ingress
    instrument(LarkPlatformAdapter, "_parse_message_components", "T3")
    instrument(LarkPlatformAdapter, "_download_message_resource", "T9", "native_media_download")

    class ObservedLatency(lark_latency.LarkLatency):
        def __init__(self, raw, received, *, native_observed=True):
            super().__init__(raw, received, native_observed=native_observed)
            self.probe = active.get() or Trace(writer, raw)
            self.probe.queued = True

        def mark(self, step):
            if step not in self.times and step in MARKS:
                self.probe.emit(MARKS[step], step, "point")
            super().mark(step)

        def log(self):
            # Gateway finalization owns the summary, including early exits.
            pass

    lark_latency.LarkLatency = ObservedLatency
    submitted = lark_latency.submitted

    @contextmanager
    def classified(event, *, streaming=False, progress=False):
        token = delivery.set("status" if progress else "streaming" if streaming else "answer")
        try:
            with submitted(event, streaming=streaming, progress=progress):
                yield
        finally:
            delivery.reset(token)
    lark_latency.submitted = classified

    def instrument_send(owner, method, visible):
        original = getattr(owner, method)
        @wraps(original)
        async def send(self, request, *args, **kwargs):
            trace = active.get()
            if trace is None:
                return await original(self, request, *args, **kwargs)
            kind = delivery.get() or "answer"
            if kind == "streaming":
                body = getattr(request, "request_body", None)
                kind = ("answer_shell" if owner is Card or owner is Message and
                        getattr(body, "msg_type", None) == "interactive" else "answer")
            route = getattr(request, "receive_id_type", None)
            if route not in {"chat_id", "open_id"}:
                route = "reply" if method == "areply" else "card_or_default"
            attempt = "retry_or_fallback" if kind in trace.send_failed else "submit"
            if attempt != "submit":
                trace.emit("T19", attempt, "point", delivery_kind=kind, route=route)
            name = f"{owner.__name__}.{method}"
            with span("T18", name, delivery_kind=kind, route=route, attempt=attempt) as op:
                try:
                    response = await original(self, request, *args, **kwargs)
                except BaseException as exc:
                    trace.send_failed.add(kind)
                    trace.emit("T19", "send_exception", "point", up=op.ident,
                               status="cancelled" if isinstance(exc, asyncio.CancelledError) else "error",
                               delivery_kind=kind)
                    raise
                if not response.success():
                    trace.send_failed.add(kind)
                    op.status = "rejected"
                    trace.emit("T19", "send_rejected", "point", up=op.ident, status="rejected",
                               response_code=response.code, delivery_kind=kind)
                else:
                    trace.send_failed.discard(kind)
                    if visible:
                        trace.emit("T20", "send_api_success", "point", up=op.ident, delivery_kind=kind)
                return response
        setattr(owner, method, send)

    for owner, method, visible in ((Message, "acreate", True), (Message, "areply", True),
                                   (Card, "acreate", False), (Card, "asettings", False),
                                   (CardElement, "acontent", True)):
        instrument_send(owner, method, visible)

    from fat_fish.gateway.platform_gateway import PlatformGateway
    from fat_fish.communication.runtime import CommunicationRuntime
    from fat_fish.adapters.lark_communication import LarkCommunication
    from fat_fish.adapters.lark_retraction import LarkRetractions
    from fat_fish.gateway.chat_turn import ChatTurns, Turn
    from fat_fish.gateway.reply_schedule import ReplySchedule
    from fat_fish.memory.gateway_bridge import GatewayMemory
    from fat_fish.memory.service import MemoryService
    from fat_fish.commands.registry import CommandRegistry
    from fat_fish.integrations.deepseek_chatbot import DeepSeekChatBot
    from fat_fish.adapters.group_media import GroupMedia

    run = PlatformGateway.run
    @wraps(run)
    async def gateway(self, event, downstream):
        timeline = lark_latency.trace(event)
        if not isinstance(timeline, ObservedLatency):
            return await run(self, event, downstream)
        trace = timeline.probe
        token = active.set(trace)
        status = "complete"
        trace.emit("T4", "gateway_received", "point")
        try:
            return await run(self, event, downstream)
        except BaseException as exc:
            status = "cancelled" if isinstance(exc, asyncio.CancelledError) else "error"
            raise
        finally:
            trace.summary(timeline.outcome or status, timeline.tokens)
            active.reset(token)
    PlatformGateway.run = gateway

    for owner, method, stage, label in (
        (CommunicationRuntime, "admit_async", "T5", "admission"),
        (CommunicationRuntime, "authorize", "T17", "authority_check"),
        (LarkRetractions, "track", "T6", "retraction_registration"),
        (LarkRetractions, "reconcile", "T6", "retraction_reconciliation"),
        (ChatTurns, "admit", "T7", "turn_admission_and_batch"),
        (Turn, "ready", "T7", "turn_wait"),
        (GatewayMemory, "prepare", "T8", "memory_prepare"),
        (GatewayMemory, "augment", "T8", "memory_recall"),
        (GatewayMemory, "sent", "T8", "memory_write"),
        (LarkCommunication, "prepare_media", "T9", "media_prepare"),
        (GroupMedia, "transcribe", "T9", "voice_prepare"),
        (CommandRegistry, "dispatch", "T10", "command_dispatch"),
        (PlatformGateway, "_participate", "T10", "participation_route"),
        (ReplySchedule, "wait", "T16", "random_reply_wait"),
        (LarkCommunication, "reply_native", "T17", "reply_route"),
        (LarkMessageEvent, "_fallback_send_streaming", "T19", "streaming_fallback"),
    ):
        instrument(owner, method, stage, label)
    instrument(__import__(PlatformGateway.__module__, fromlist=["upload_attachments"]),
               "upload_attachments", "T9", "document_prepare")

    from fat_fish.integrations import web_search
    from fat_fish.commands import agent_tool
    for module, method, capability in ((web_search, "offer_search", "web"), (agent_tool, "offer_management", "management")):
        original = getattr(module, method)
        if capability == "web":
            @wraps(original)
            def offer_web(event, request, *args, _original=original, **kwargs):
                result = _original(event, request, *args, **kwargs)
                if trace := active.get():
                    available = web_search.WEB_SEARCH_TOOL in event.get_extra("_ffa_native_tools", {})
                    trace.emit("T10", "web_availability", "point", enabled=bool(kwargs.get("enabled", True)),
                               offered=available)
                return result
            module.offer_search = offer_web
        else:
            @wraps(original)
            async def offer_management(event, request, *args, _original=original, **kwargs):
                result = await _original(event, request, *args, **kwargs)
                if trace := active.get():
                    trace.emit("T10", "management_availability", "point",
                               offered=agent_tool.TOOL_NAME in event.get_extra("_ffa_native_tools", {}))
                return result
            module.offer_management = offer_management

    writer_lock = MemoryService._writer
    @contextmanager
    def acquire_lock(self, write, timeout):
        if not write:
            yield
            return
        with ExitStack() as stack:
            with span("T8", "memory_writer_lock_wait"):
                stack.enter_context(writer_lock(self, write, timeout))
            yield
    MemoryService._writer = acquire_lock

    sqlite_connect = sqlite3.connect

    class ObservedConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if sql.upper() not in {"BEGIN", "BEGIN IMMEDIATE"}:
                return super().execute(sql, parameters)
            stage = active_stage.get() or "T6"
            with span(stage, "sqlite_begin_immediate" if sql.upper().endswith("IMMEDIATE") else "sqlite_begin") as op:
                try:
                    return super().execute(sql, parameters)
                except sqlite3.OperationalError as exc:
                    if op and getattr(exc, "sqlite_errorcode", None) in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
                        op.fields["database_busy"] = True
                    raise

    @wraps(sqlite_connect)
    def connect(*args, **kwargs):
        if active.get() is not None and len(args) < 6:
            kwargs.setdefault("factory", ObservedConnection)
        return sqlite_connect(*args, **kwargs)
    sqlite3.connect = connect

    def provider_scope(owner, method, kind):
        original = getattr(owner, method)
        @wraps(original)
        async def request(self, *args, **kwargs):
            if active.get() is None:
                return await original(self, *args, **kwargs)
            operation = kind
            if kind == "chatbot":
                verb, path = args[:2]
                operation = {"POST/files": "file_upload", "POST/v1/tools/asr": "asr",
                             "POST/chat/completions": "native_web"}.get(verb + path, "file_delete")
                if operation == "native_web":
                    tools = (kwargs.get("json_body") or {}).get("tools") or []
                    tool = tools[0].get("type") if tools else None
                    if tool in {"deepseek_web_search", "deepseek_read_url"}:
                        operation = tool
            token = api.set(operation)
            try:
                with span("T14", operation) as op:
                    result = await original(self, *args, **kwargs)
                    if op and kind == "chatbot" and isinstance(result, dict):
                        usage = result.get("usage") or {}
                        op.fields.update({field: usage[field] for field in
                                          ("prompt_tokens", "prompt_cache_hit_tokens", "completion_tokens")
                                          if isinstance(usage.get(field), int)})
                    return result
            finally:
                api.reset(token)
        setattr(owner, method, request)

    provider_scope(DeepSeekChatBot, "_request", "chatbot")
    provider_scope(ProviderOpenAIOfficial, "_query", "main_agent_model")

    stream = ProviderOpenAIOfficial._query_stream
    @wraps(stream)
    async def query_stream(self, *args, **kwargs):
        if active.get() is None:
            async for chunk in stream(self, *args, **kwargs):
                yield chunk
            return
        with span("T14", "main_agent_model_stream"):
            iterator = stream(self, *args, **kwargs)
            try:
                while True:
                    token = api.set("main_agent_model_stream")
                    try:
                        chunk = await anext(iterator)
                    except StopAsyncIteration:
                        break
                    finally:
                        api.reset(token)
                    yield chunk
            finally:
                await iterator.aclose()
    ProviderOpenAIOfficial._query_stream = query_stream

    def http_attempt(owner, method):
        original = getattr(owner, method)
        @wraps(original)
        async def request(self, *args, **kwargs):
            kind = api.get()
            if not kind:
                return await original(self, *args, **kwargs)
            with span("T14", "http_attempt", api_operation=kind) as op:
                response = await original(self, *args, **kwargs)
                code = getattr(response, "status_code", getattr(response, "status", None))
                if op and isinstance(code, int):
                    op.fields["response_code"] = code
                    if code >= 400:
                        op.status = "rejected"
                return response
        setattr(owner, method, request)

    http_attempt(aiohttp.ClientSession, "_request")
    http_attempt(httpx.AsyncClient, "send")

    execute = FunctionToolExecutor.execute
    @classmethod
    async def tool_execution(cls, tool, run_context, **arguments):
        name = tool.name if tool.name in {"ffa_manage", "ffa_web_search", "ffa_read_url"} else "other"
        with span("T13", "tool_execution", tool=name) as op:
            async for result in execute(tool, run_context, **arguments):
                if op and getattr(result, "isError", False):
                    op.status = "error"
                yield result
    FunctionToolExecutor.execute = tool_execution

    route_tools = ToolLoopAgentRunner._handle_function_tools
    @wraps(route_tools)
    async def selected_tools(self, request, response):
        names = [name if name in {"ffa_manage", "ffa_web_search", "ffa_read_url"} else "other"
                 for name in response.tools_call_name]
        with span("T13", "selected_tools", tools=names):
            async for result in route_tools(self, request, response):
                yield result
    ToolLoopAgentRunner._handle_function_tools = selected_tools
