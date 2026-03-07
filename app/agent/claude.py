import json
import anthropic
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.agent.tools import TOOLS
from app.agent.executor import execute_tool

settings = get_settings()

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
    Run a Claude conversation turn with tool use.
    Returns (assistant_text, updated_messages).
    """
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    system = f"{SYSTEM_PROMPT}\n\n{character_context}"
    updated_messages = list(messages)

    while True:
        response = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            system=system,
            tools=TOOLS,
            messages=updated_messages,
        )

        # Collect text and tool use blocks
        tool_uses = [b for b in response.content if b.type == "tool_use"]
        text_blocks = [b for b in response.content if b.type == "text"]

        # Convert SDK content blocks to plain dicts so they survive JSON serialization
        content_dicts = [block.model_dump() for block in response.content]
        updated_messages.append({"role": "assistant", "content": content_dicts})

        if response.stop_reason == "end_turn" or not tool_uses:
            final_text = " ".join(b.text for b in text_blocks)
            return final_text, updated_messages

        # Execute all tool calls
        tool_results = []
        for tool_use in tool_uses:
            result = await execute_tool(tool_use.name, tool_use.input, db)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tool_use.id,
                "content": result,
            })

        updated_messages.append({"role": "user", "content": tool_results})
