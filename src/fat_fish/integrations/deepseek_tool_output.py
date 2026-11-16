"""Keep managed Agent tool protocol inside the native runner."""
from contextlib import aclosing
import logging
import re

from fat_fish.integrations.web_search import DSML_MARKER, clean_message, clean_reply


_PARTIAL_MARKER = re.compile(r"</?(?:[｜|]+(?:D(?:S(?:M(?:L)?)?)?)?)?\Z", re.I)
_RETRY_PROMPT = (
    "请重新处理当前用户请求。需要使用工具时，通过 API 提供的工具结构实际调用，使用它当前的参数定义。"
    "不要把工具名称、标签、参数或 JSON 指令写进用户可见正文。"
)
_MANAGED_TOOLS = {"ffa_manage": "管理指令", "web_search_deepseek": "联网搜索", "deepseek_fetch_web_page": "联网搜索"}
logger = logging.getLogger(__name__)


def private_tool_log(record):
    if not record.pathname.replace("\\", "/").endswith("/tool_loop_agent_runner.py"):
        return True
    text = record.getMessage()
    return not any(text.startswith((f"使用工具：{name}，参数：", f"Tool `{name}` Result:"))
                   for name in _MANAGED_TOOLS)


def enforce_offered_tools(response, offered, event):
    unavailable = {name for name in response.tools_call_name if name in _MANAGED_TOOLS and name not in offered}
    if not unavailable:
        return
    calls = [(name, args, ident) for name, args, ident in zip(
        response.tools_call_name, response.tools_call_args, response.tools_call_ids) if name not in unavailable]
    response.tools_call_name = [name for name, _, _ in calls]
    response.tools_call_args = [args for _, args, _ in calls]
    response.tools_call_ids = [ident for _, _, ident in calls]
    if not calls:
        response.role = "assistant"
        response.result_chain = None
        features = "、".join(sorted({_MANAGED_TOOLS[name] for name in unavailable}))
        response.completion_text = user_feedback(event, f"本轮无法使用{features}，请求未执行。")
    logger.info("FFA ignored unoffered native tool names=%s", ",".join(sorted(unavailable)))


def clean_assistant_history(messages):
    """Remove stale protocol from assistant history while preserving real tool pairs."""
    clean = []
    for message in messages:
        content = message.content
        if message.role != "assistant":
            clean.append(message)
            continue
        if isinstance(content, str) and DSML_MARKER.search(content):
            content = clean_reply(content)
        elif isinstance(content, list):
            parts = []
            for part in content:
                if part.type != "text" or not DSML_MARKER.search(part.text):
                    parts.append(part)
                elif body := clean_reply(part.text):
                    parts.append(part.model_copy(update={"text": body}))
            content = parts
        if content or message.tool_calls:
            clean.append(message.model_copy(update={"content": content}) if content is not message.content else message)
    return clean


class ToolText:
    """Hold a possible protocol prefix when it spans streaming chunks."""
    def __init__(self):
        self.pending = ""
        self.detected = False

    def consume(self, text):
        if self.detected:
            return ""
        text = self.pending + text
        self.pending = ""
        if match := DSML_MARKER.search(text):
            self.detected = True
            return text[:match.start()]
        start = text.rfind("<")
        if start >= 0 and _PARTIAL_MARKER.fullmatch(text[start:]):
            self.pending = text[start:]
            return text[:start]
        return text


def install_deepseek_tool_output():
    from astrbot.core.agent.message import SystemMessageSegment
    from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner

    original = ToolLoopAgentRunner._iter_llm_responses
    native_logger = logging.getLogger("astrbot")
    native_logger.addFilter(private_tool_log)

    async def boundary(runner, *, include_model=True):
        context = runner.run_context
        event = getattr(context.context, "event", None)
        if event is None or event.get_extra("_ffa_platform_admission") is None:
            async with aclosing(original(runner, include_model=include_model)) as responses:
                async for response in responses:
                    yield response
            return

        toolset = runner._func_tool_for_provider()
        offered = set(toolset.names()) if toolset else set()
        managed = sorted(offered & _MANAGED_TOOLS.keys())
        modalities = runner.provider.provider_config.get("modalities")
        tool_capability = not isinstance(modalities, list) or not modalities or "tool_use" in modalities
        manifest = tuple(managed), tool_capability
        if event.get_extra("_ffa_native_tool_manifest") != manifest:
            event.set_extra("_ffa_native_tool_manifest", manifest)
            logger.info("FFA native tools offered=%s tool_use=%s", ",".join(managed) or "-", tool_capability)

        stream = ToolText()
        async with aclosing(original(runner, include_model=include_model)) as responses:
            async for response in responses:
                text = response.completion_text or ""
                if response.is_chunk:
                    visible = stream.consume(text)
                    if visible:
                        response.completion_text = visible
                        yield response
                    continue
                if stream.detected or DSML_MARKER.search(text):
                    if (response.role == "assistant" and not response.tools_call_name
                            and managed
                            and not event.get_extra("_ffa_tool_protocol_retry")):
                        break
                    redact(response, event)
                enforce_offered_tools(response, offered, event)
                yield response
            else:
                return

        event.set_extra("_ffa_tool_protocol_retry", True)
        logger.info("FFA retried malformed native tool output")
        # Only structured native calls reach authorization. The malformed
        # response never becomes context for the retry.
        notice = SystemMessageSegment(content=_RETRY_PROMPT)
        context.messages.insert(0, notice)
        try:
            async with aclosing(original(runner, include_model=include_model)) as responses:
                async for response in responses:
                    if not response.is_chunk:
                        if DSML_MARKER.search(response.completion_text or ""):
                            redact(response, event)
                        enforce_offered_tools(response, offered, event)
                        yield response
        finally:
            context.messages[:] = [message for message in context.messages if message is not notice]

    ToolLoopAgentRunner._iter_llm_responses = boundary

    def restore():
        if ToolLoopAgentRunner._iter_llm_responses is boundary:
            ToolLoopAgentRunner._iter_llm_responses = original
            native_logger.removeFilter(private_tool_log)

    return restore


def redact(response, event):
    if response.result_chain is not None:
        clean_message(response.result_chain)
    else:
        response.completion_text = clean_reply(response.completion_text or "")
    if not response.completion_text and not response.tools_call_name and not getattr(response.result_chain, "chain", None):
        response.completion_text = user_feedback(event, "这次没能完成工具调用，麻烦再试一下。")


def user_feedback(event, text):
    from fat_fish.persona.private_persona import apply_affix

    return apply_affix(text, event.get_extra("_ffa_persona_notes", ""))
