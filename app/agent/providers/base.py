from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict


@dataclass
class StreamEvent:
    """A single event from a streaming provider response."""
    type: str  # "text" | "tool_use" | "usage" | "stop"
    content: str = ""                        # type=text
    tool_call: Optional["ToolCall"] = None  # type=tool_use
    input_tokens: int = 0                   # type=usage
    output_tokens: int = 0                  # type=usage
    stop_reason: str = ""                   # type=stop
    raw_content: list = field(default_factory=list)  # type=stop


@dataclass
class LLMResponse:
    """
    Unified response from any LLM provider.
    raw_assistant_content is always in Anthropic list-of-blocks format
    so it can be stored in the DB and replayed to either provider.
    """
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str = "end_turn"
    raw_assistant_content: list[dict] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def has_tool_calls(self) -> bool:
        return len(self.tool_calls) > 0


class BaseLLMProvider(ABC):
    @abstractmethod
    async def create_message(
        self,
        messages: list[dict],
        system: str,
        tools: list[dict],
    ) -> LLMResponse:
        """
        Messages and tools are always in Anthropic format.
        Providers convert internally as needed.
        Returns LLMResponse with raw_assistant_content in Anthropic format.
        """
        ...

    async def stream_message(self, messages: list[dict], system: str, tools: list[dict]):
        """
        Async generator yielding StreamEvents.
        Subclasses should override this for streaming support.
        Events:  text → content delta
                 tool_use → complete tool call (after stream ends)
                 usage → token counts
                 stop → end of stream with raw_content for history
        Default: falls back to create_message and yields synthetic events.
        """
        response = await self.create_message(messages, system, tools)
        # Yield text in small chunks to simulate streaming
        chunk_size = 8
        for i in range(0, len(response.text), chunk_size):
            yield StreamEvent(type="text", content=response.text[i:i + chunk_size])
        for tc in response.tool_calls:
            yield StreamEvent(type="tool_use", tool_call=tc)
        yield StreamEvent(type="usage", input_tokens=response.input_tokens, output_tokens=response.output_tokens)
        yield StreamEvent(type="stop", stop_reason=response.stop_reason, raw_content=response.raw_assistant_content)
