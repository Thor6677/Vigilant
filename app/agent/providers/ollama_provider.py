"""
Ollama provider using the OpenAI-compatible API.
Converts Anthropic-format messages/tools to OpenAI format and back.
"""
import json
import re
import logging
from openai import AsyncOpenAI

log = logging.getLogger(__name__)
from app.agent.providers.base import BaseLLMProvider, LLMResponse, StreamEvent, ToolCall
from app.config import get_settings

settings = get_settings()


def _tools_to_openai(tools: list[dict]) -> list[dict]:
    """Convert Anthropic tool definitions to OpenAI format."""
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
            },
        }
        for t in tools
    ]


def _messages_to_openai(messages: list[dict]) -> list[dict]:
    """
    Convert Anthropic-format messages to OpenAI format.

    Anthropic format:
      - user/assistant roles
      - content is either a string or list of blocks
      - tool_result blocks carry tool output back to the model
      - tool_use blocks are assistant function calls

    OpenAI format:
      - user/assistant/tool roles
      - tool calls on assistant messages via tool_calls list
      - tool results as separate messages with role="tool"
    """
    out = []
    for msg in messages:
        role = msg["role"]
        content = msg["content"]

        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue

        if role == "assistant":
            # Content is a list of text/tool_use blocks
            text_parts = [b["text"] for b in content if b.get("type") == "text"]
            tool_use_blocks = [b for b in content if b.get("type") == "tool_use"]

            oai_msg: dict = {"role": "assistant", "content": " ".join(text_parts) or None}

            if tool_use_blocks:
                oai_msg["tool_calls"] = [
                    {
                        "id": b["id"],
                        "type": "function",
                        "function": {
                            "name": b["name"],
                            "arguments": json.dumps(b["input"]),
                        },
                    }
                    for b in tool_use_blocks
                ]

            out.append(oai_msg)

        elif role == "user":
            # May be a list containing tool_result blocks
            if isinstance(content, list) and content and content[0].get("type") == "tool_result":
                for block in content:
                    out.append({
                        "role": "tool",
                        "tool_call_id": block["tool_use_id"],
                        "content": block["content"],
                    })
            else:
                # Regular user message — flatten text blocks
                if isinstance(content, list):
                    text = " ".join(b.get("text", "") for b in content if b.get("type") == "text")
                else:
                    text = str(content)
                out.append({"role": "user", "content": text})

    return out


def _response_to_anthropic_content(choice) -> list[dict]:
    """
    Convert an OpenAI response choice back to Anthropic raw_assistant_content format
    so it can be stored in the DB and replayed consistently.
    """
    blocks = []
    msg = choice.message

    if msg.content:
        blocks.append({"type": "text", "text": msg.content})

    if msg.tool_calls:
        for tc in msg.tool_calls:
            try:
                inp = json.loads(tc.function.arguments)
            except json.JSONDecodeError:
                inp = {}
            blocks.append({
                "type": "tool_use",
                "id": tc.id,
                "name": tc.function.name,
                "input": inp,
            })

    return blocks


class OllamaProvider(BaseLLMProvider):
    def __init__(self):
        self.client = AsyncOpenAI(
            base_url=settings.ollama_base_url,
            api_key="ollama",  # Ollama ignores the key but openai client requires it
        )
        self.model = settings.ollama_model

    async def create_message(self, messages, system, tools) -> LLMResponse:
        oai_messages = [{"role": "system", "content": system}] + _messages_to_openai(messages)
        oai_tools = _tools_to_openai(tools) if tools else None

        kwargs = {
            "model": self.model,
            "messages": oai_messages,
            "max_tokens": 4096,
        }
        if oai_tools:
            kwargs["tools"] = oai_tools
            kwargs["tool_choice"] = "auto"

        response = await self.client.chat.completions.create(**kwargs)
        choice = response.choices[0]
        msg = choice.message

        # Strip qwen3 thinking blocks if present
        raw_text = msg.content or ""
        text = re.sub(r"<think>.*?</think>", "", raw_text, flags=re.DOTALL).strip()
        tool_calls = []
        if msg.tool_calls:
            for tc in msg.tool_calls:
                try:
                    inp = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    inp = {}
                tool_calls.append(ToolCall(id=tc.id, name=tc.function.name, input=inp))

        # Map OpenAI finish_reason → Anthropic stop_reason
        finish = choice.finish_reason or "stop"
        stop_reason = "tool_use" if finish == "tool_calls" else "end_turn"

        raw_assistant_content = _response_to_anthropic_content(choice)

        usage = response.usage
        return LLMResponse(
            text=text,
            tool_calls=tool_calls,
            stop_reason=stop_reason,
            raw_assistant_content=raw_assistant_content,
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
        )

    async def stream_message(self, messages, system, tools):
        """Stream response from Ollama via OpenAI-compatible streaming API."""
        oai_messages = [{"role": "system", "content": system}] + _messages_to_openai(messages)
        oai_tools = _tools_to_openai(tools) if tools else None

        kwargs = {
            "model": self.model,
            "messages": oai_messages,
            "max_tokens": 4096,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if oai_tools:
            kwargs["tools"] = oai_tools
            kwargs["tool_choice"] = "auto"

        # Accumulate tool call arguments across chunks (keyed by index)
        tool_acc: dict[int, dict] = {}
        accumulated_text = ""

        try:
            stream = await self.client.chat.completions.create(**kwargs)
            async for chunk in stream:
                # Usage chunk (final chunk from Ollama)
                if chunk.usage:
                    yield StreamEvent(
                        type="usage",
                        input_tokens=chunk.usage.prompt_tokens or 0,
                        output_tokens=chunk.usage.completion_tokens or 0,
                    )

                if not chunk.choices:
                    continue

                choice = chunk.choices[0]
                delta = choice.delta

                # Text delta — strip thinking blocks progressively
                if delta.content:
                    text = re.sub(r"<think>.*?</think>", "", delta.content, flags=re.DOTALL)
                    if text:
                        accumulated_text += text
                        yield StreamEvent(type="text", content=text)

                # Tool call deltas — accumulate arguments by index
                if delta.tool_calls:
                    for tc_delta in delta.tool_calls:
                        idx = tc_delta.index
                        if idx not in tool_acc:
                            tool_acc[idx] = {"id": "", "name": "", "arguments": ""}
                        if tc_delta.id:
                            tool_acc[idx]["id"] = tc_delta.id
                        if tc_delta.function:
                            if tc_delta.function.name:
                                tool_acc[idx]["name"] = tc_delta.function.name
                            if tc_delta.function.arguments:
                                tool_acc[idx]["arguments"] += tc_delta.function.arguments

        except Exception as e:
            log.error("Ollama stream error: %s", e)
            raise

        # Yield complete tool calls
        for idx in sorted(tool_acc.keys()):
            tc = tool_acc[idx]
            try:
                inp = json.loads(tc["arguments"])
            except json.JSONDecodeError:
                inp = {}
            yield StreamEvent(
                type="tool_use",
                tool_call=ToolCall(id=tc["id"], name=tc["name"], input=inp),
            )

        # Build raw_content in Anthropic format for history
        raw_content = []
        if accumulated_text:
            raw_content.append({"type": "text", "text": accumulated_text})
        for idx in sorted(tool_acc.keys()):
            tc = tool_acc[idx]
            try:
                inp = json.loads(tc["arguments"])
            except json.JSONDecodeError:
                inp = {}
            raw_content.append({"type": "tool_use", "id": tc["id"], "name": tc["name"], "input": inp})

        stop_reason = "tool_use" if tool_acc else "end_turn"
        yield StreamEvent(type="stop", stop_reason=stop_reason, raw_content=raw_content)
