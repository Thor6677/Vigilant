import json
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.agent.tools import TOOLS
from app.agent.executor import execute_tool
from app.agent.providers import get_provider

SYSTEM_PROMPT = """You are AURA, an intelligent AI assistant integrated with EVE Online's ESI API.
You help capsuleers (EVE Online players) manage their characters, assets, industry, and market activities.

You have access to tools that can query real-time EVE Online data:
- Character locations and ship status
- Asset and inventory management across stations and structures
- Industry job tracking (manufacturing, research, reactions)
- Market prices and order books across trade hubs
- Clone and jump clone locations
- Route calculation between systems
- Corporation and alliance information

When answering:
- Be concise and practical — capsuleers are busy pilots
- Format ISK values with commas (e.g. 1,234,567.89 ISK)
- Mention system security status when relevant (0.5+ = highsec, 0.1-0.4 = lowsec, 0.0 = nullsec)
- If an asset search returns many results, summarize and highlight the most relevant ones
- For industry jobs, show time remaining if end_date is available
- Always clarify which character you're querying when multiple characters are available

The available characters are provided in each message context."""


async def chat(
    messages: list[dict],
    character_context: str,
    db: AsyncSession,
) -> tuple[str, list[dict], dict]:
    """
    Run a conversation turn with tool use via the configured LLM provider.
    Returns (assistant_text, updated_messages, stats).
    stats keys: input_tokens, output_tokens, tool_calls, model
    """
    provider = get_provider()
    settings = get_settings()
    system = f"{SYSTEM_PROMPT}\n\n{character_context}"
    updated_messages = list(messages)
    total_input = 0
    total_output = 0
    total_tool_calls = 0

    while True:
        response = await provider.create_message(
            messages=updated_messages,
            system=system,
            tools=TOOLS,
        )

        total_input += response.input_tokens
        total_output += response.output_tokens

        updated_messages.append({
            "role": "assistant",
            "content": response.raw_assistant_content,
        })

        if not response.has_tool_calls or response.stop_reason == "end_turn":
            model = (
                settings.anthropic_model
                if settings.llm_provider.lower() == "anthropic"
                else settings.ollama_model
            )
            stats = {
                "input_tokens": total_input,
                "output_tokens": total_output,
                "total_tokens": total_input + total_output,
                "tool_calls": total_tool_calls,
                "model": model,
                "provider": settings.llm_provider,
            }
            return response.text, updated_messages, stats

        tool_results = []
        for tc in response.tool_calls:
            result = await execute_tool(tc.name, tc.input, db)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tc.id,
                "content": result,
            })
            total_tool_calls += 1

        updated_messages.append({"role": "user", "content": tool_results})
