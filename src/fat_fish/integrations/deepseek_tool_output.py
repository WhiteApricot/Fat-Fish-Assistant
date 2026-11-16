"""Recover DeepSeek tool protocol emitted in assistant text using native calls."""
from contextlib import aclosing
import re

from fat_fish.integrations.web_search import DSML_MARKER, clean_message, clean_reply


_PARTIAL_MARKER = re.compile(r"</?(?:[｜|]+(?:D(?:S(?:M(?:L)?)?)?)?)?\Z", re.I)
_RETRY_PROMPT = (
    "请重新处理当前用户请求。需要使用工具时，通过 API 提供的工具结构实际调用，使用它当前的参数定义。"
    "不要把工具名称、标签、参数或 JSON 指令写进用户可见正文。"
)


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

    async def boundary(runner, *, include_model=True):
        context = runner.run_context
        event = getattr(context.context, "event", None)
        endpoint = runner.provider.provider_config.get("api_base") or ""
        if (event is None or event.get_extra("_ffa_platform_admission") is None
                or not isinstance(endpoint, str) or not endpoint.startswith("https://")
                or endpoint[8:].partition("/")[0].lower() not in {"api.deepseek.com", "api.deepseek.com:443"}):
            async with aclosing(original(runner, include_model=include_model)) as responses:
                async for response in responses:
                    yield response
            return

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
                            and runner._func_tool_for_provider()
                            and not event.get_extra("_ffa_tool_protocol_retry")):
                        break
                    redact(response, event)
                yield response
            else:
                return

        event.set_extra("_ffa_tool_protocol_retry", True)
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
                        yield response
        finally:
            context.messages[:] = [message for message in context.messages if message is not notice]

    ToolLoopAgentRunner._iter_llm_responses = boundary

    def restore():
        if ToolLoopAgentRunner._iter_llm_responses is boundary:
            ToolLoopAgentRunner._iter_llm_responses = original

    return restore


def redact(response, event):
    if response.result_chain is not None:
        clean_message(response.result_chain)
    else:
        response.completion_text = clean_reply(response.completion_text or "")
    if not response.completion_text and not response.tools_call_name and not getattr(response.result_chain, "chain", None):
        from fat_fish.persona.private_persona import apply_affix

        response.completion_text = apply_affix(
            "这次没能完成工具调用，麻烦再试一下。", event.get_extra("_ffa_persona_notes", ""))
