import anthropic
from app.agent.providers.base import BaseLLMProvider, LLMResponse, ToolCall
from app.config import get_settings

settings = get_settings()


class AnthropicProvider(BaseLLMProvider):
    def __init__(self):
        self.client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        self.model = settings.anthropic_model

    async def create_message(self, messages, system, tools) -> LLMResponse:
        response = await self.client.messages.create(
            model=self.model,
            max_tokens=4096,
            system=system,
            tools=tools,
            messages=messages,
        )

        text = " ".join(b.text for b in response.content if b.type == "text")
        tool_calls = [
            ToolCall(id=b.id, name=b.name, input=b.input)
            for b in response.content if b.type == "tool_use"
        ]
        raw_assistant_content = [b.model_dump() for b in response.content]

        return LLMResponse(
            text=text,
            tool_calls=tool_calls,
            stop_reason=response.stop_reason or "end_turn",
            raw_assistant_content=raw_assistant_content,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )
