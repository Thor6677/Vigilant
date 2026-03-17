import anthropic
from app.agent.providers.base import BaseLLMProvider, LLMResponse, StreamEvent, ToolCall
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

    async def stream_message(self, messages, system, tools):
        """Stream response, yielding text deltas then tool calls and usage at the end."""
        async with self.client.messages.stream(
            model=self.model,
            max_tokens=4096,
            system=system,
            tools=tools,
            messages=messages,
        ) as stream:
            # Yield input token count as soon as we have it
            input_tokens_sent = False
            async for event in stream:
                if event.type == "message_start" and not input_tokens_sent:
                    yield StreamEvent(
                        type="usage",
                        input_tokens=event.message.usage.input_tokens,
                        output_tokens=0,
                    )
                    input_tokens_sent = True

                elif event.type == "content_block_delta":
                    if hasattr(event.delta, "text") and event.delta.text:
                        yield StreamEvent(type="text", content=event.delta.text)

            # Get the final message for tool calls, raw content, and actual usage
            final = await stream.get_final_message()

        # Yield complete tool calls (inputs only arrive fully at the end)
        for block in final.content:
            if block.type == "tool_use":
                yield StreamEvent(
                    type="tool_use",
                    tool_call=ToolCall(id=block.id, name=block.name, input=block.input),
                )

        yield StreamEvent(
            type="usage",
            input_tokens=final.usage.input_tokens,
            output_tokens=final.usage.output_tokens,
        )

        raw_content = [b.model_dump() for b in final.content]
        yield StreamEvent(
            type="stop",
            stop_reason=final.stop_reason or "end_turn",
            raw_content=raw_content,
        )
