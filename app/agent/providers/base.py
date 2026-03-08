from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict


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
