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

IMPORTANT RULES — follow these before answering:
1. If the user mentions a name you don't immediately recognise as a solar system or region, call search_item_types with that name first. Never say a name doesn't exist without checking.
2. If search_item_types returns matches, use find_item_in_assets to search each character's assets for it.
3. If search_item_types returns multiple distinct matches (e.g. "Proteus" vs "Proteus Blueprint"), ask the user which one they mean before searching assets.
4. Only after search_item_types and find_item_in_assets both return nothing should you tell the user nothing was found.

When answering:
- Never show raw IDs (type_id, location_id, structure_id, character_id, etc.) anywhere in your response — not even in parentheses or as a note. If a name cannot be resolved, say "an unknown location" or "an unidentified structure" and nothing more.
- Never mention tool names (like get_character_location, find_item_in_assets) in your responses. You are a conversational assistant — just answer naturally. If you need more information, ask a plain follow-up question.
- Maintain conversation context. If the user just asked about pricing and follows up with "what about X?", they are asking about the price of X — not its location or other attributes.
- Always state which character an asset belongs to
- Be concise and practical — capsuleers are busy pilots
- Format ISK values with commas (e.g. 1,234,567.89 ISK)
- Mention system security status when relevant (0.5+ = highsec, 0.1-0.4 = lowsec, 0.0 = nullsec)
- If an asset search returns many results, summarize and highlight the most relevant ones
- For industry jobs, show time remaining if end_date is available
- Always clarify which character you're querying when multiple characters are available

The available characters are provided in each message context."""


def _active_model() -> str:
    settings = get_settings()
    if settings.llm_provider.lower() == "anthropic":
        return settings.anthropic_model
    return settings.ollama_model


async def chat(
    messages: list[dict],
    character_context: str,
    db: AsyncSession,
) -> tuple[str, list[dict], dict]:
    """
    Non-streaming chat turn. Returns (assistant_text, updated_messages, stats).
    """
    provider = get_provider()
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
            stats = {
                "input_tokens": total_input,
                "output_tokens": total_output,
                "total_tokens": total_input + total_output,
                "tool_calls": total_tool_calls,
                "model": _active_model(),
                "provider": get_settings().llm_provider,
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


async def stream_chat(
    messages: list[dict],
    character_context: str,
    db: AsyncSession,
):
    """
    Streaming chat turn. Async generator yielding SSE-ready dicts.

    Event types:
      {"type": "text",   "content": "...", "est_output_tokens": N}
      {"type": "tool",   "name": "...", "status": "running"}
      {"type": "tokens", "input": N, "output": N, "total": N}
      {"type": "done",   "messages": [...], "stats": {...}}
    """
    provider = get_provider()
    settings = get_settings()
    system = f"{SYSTEM_PROMPT}\n\n{character_context}"
    updated_messages = list(messages)
    total_input = 0
    total_output = 0
    total_tool_calls = 0
    output_chars = 0

    while True:
        current_tool_calls = []
        current_raw_content = []
        stop_reason = "end_turn"

        async for event in provider.stream_message(updated_messages, system, TOOLS):
            if event.type == "text":
                output_chars += len(event.content)
                yield {
                    "type": "text",
                    "content": event.content,
                    "est_output_tokens": output_chars // 4,
                }

            elif event.type == "tool_use":
                current_tool_calls.append(event.tool_call)

            elif event.type == "usage":
                total_input = event.input_tokens if event.input_tokens else total_input
                if event.output_tokens:
                    total_output = event.output_tokens
                yield {
                    "type": "tokens",
                    "input": total_input,
                    "output": total_output,
                    "total": total_input + total_output,
                }

            elif event.type == "stop":
                stop_reason = event.stop_reason
                current_raw_content = event.raw_content

        updated_messages.append({
            "role": "assistant",
            "content": current_raw_content,
        })

        if not current_tool_calls:
            break

        # Execute tools and stream status
        tool_results = []
        for tc in current_tool_calls:
            yield {"type": "tool", "name": tc.name, "status": "running"}
            result = await execute_tool(tc.name, tc.input, db)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tc.id,
                "content": result,
            })
            total_tool_calls += 1

        updated_messages.append({"role": "user", "content": tool_results})

    yield {
        "type": "done",
        "messages": updated_messages,
        "stats": {
            "input_tokens": total_input,
            "output_tokens": total_output,
            "total_tokens": total_input + total_output,
            "tool_calls": total_tool_calls,
            "model": _active_model(),
            "provider": settings.llm_provider,
        },
    }
