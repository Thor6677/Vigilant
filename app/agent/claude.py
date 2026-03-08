import json
from sqlalchemy.ext.asyncio import AsyncSession

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
) -> tuple[str, list[dict]]:
    """
    Run a conversation turn with tool use via the configured LLM provider.
    Returns (assistant_text, updated_messages).
    """
    provider = get_provider()
    system = f"{SYSTEM_PROMPT}\n\n{character_context}"
    updated_messages = list(messages)

    while True:
        response = await provider.create_message(
            messages=updated_messages,
            system=system,
            tools=TOOLS,
        )

        updated_messages.append({
            "role": "assistant",
            "content": response.raw_assistant_content,
        })

        if not response.has_tool_calls or response.stop_reason == "end_turn":
            return response.text, updated_messages

        tool_results = []
        for tc in response.tool_calls:
            result = await execute_tool(tc.name, tc.input, db)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tc.id,
                "content": result,
            })

        updated_messages.append({"role": "user", "content": tool_results})
