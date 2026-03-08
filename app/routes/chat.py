import json
import time
from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.config import get_settings
from app.db.models import get_db, Character, ChatSession
from app.agent.claude import chat, stream_chat
from datetime import datetime, timezone

router = APIRouter(prefix="/chat", tags=["chat"])
templates = Jinja2Templates(directory="app/templates")
settings = get_settings()


def _active_model() -> str:
    if settings.llm_provider.lower() == "anthropic":
        return settings.anthropic_model
    return settings.ollama_model


def _build_character_context(characters: list[Character], active_char: Character) -> str:
    lines = [f"Active character: {active_char.character_name} (ID: {active_char.character_id})"]
    if active_char.corporation_name:
        lines.append(f"Corporation: {active_char.corporation_name} (ID: {active_char.corporation_id})")
    if active_char.alliance_name:
        lines.append(f"Alliance: {active_char.alliance_name} (ID: {active_char.alliance_id})")
    if len(characters) > 1:
        others = [c for c in characters if c.character_id != active_char.character_id]
        lines.append("Other linked characters: " + ", ".join(f"{c.character_name} (ID: {c.character_id})" for c in others))
    return "\n".join(lines)


@router.get("", response_class=HTMLResponse)
async def chat_page(request: Request, db: AsyncSession = Depends(get_db)):
    active_id = request.session.get("active_character_id")
    if not active_id:
        from fastapi.responses import RedirectResponse
        return RedirectResponse("/")

    character_ids = request.session.get("character_ids", [])
    result = await db.execute(select(Character).where(Character.character_id.in_(character_ids)))
    characters = result.scalars().all()
    active_char = next((c for c in characters if c.character_id == active_id), None)

    session_result = await db.execute(
        select(ChatSession).where(ChatSession.character_id == active_id)
        .order_by(ChatSession.updated_at.desc())
    )
    chat_session = session_result.scalar_one_or_none()
    history = json.loads(chat_session.messages) if chat_session else []

    display_messages = []
    for msg in history:
        if isinstance(msg.get("content"), str):
            display_messages.append({"role": msg["role"], "text": msg["content"]})
        elif isinstance(msg.get("content"), list):
            text = " ".join(
                b["text"] for b in msg["content"]
                if isinstance(b, dict) and b.get("type") == "text"
            )
            if text:
                display_messages.append({"role": msg["role"], "text": text})

    return templates.TemplateResponse("chat.html", {
        "request": request,
        "characters": characters,
        "active_char": active_char,
        "messages": display_messages,
        "llm_model": _active_model(),
        "llm_provider": settings.llm_provider,
    })


@router.post("/stream")
async def stream_message(
    request: Request,
    message: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    """SSE streaming endpoint. Returns text/event-stream."""
    active_id = request.session.get("active_character_id")
    if not active_id:
        return HTMLResponse("Not authenticated", status_code=401)

    character_ids = request.session.get("character_ids", [])
    result = await db.execute(select(Character).where(Character.character_id.in_(character_ids)))
    characters = result.scalars().all()
    active_char = next((c for c in characters if c.character_id == active_id), None)

    if not active_char:
        return HTMLResponse("Character not found", status_code=404)

    session_result = await db.execute(
        select(ChatSession).where(ChatSession.character_id == active_id)
        .order_by(ChatSession.updated_at.desc())
    )
    chat_session = session_result.scalar_one_or_none()
    history = json.loads(chat_session.messages) if chat_session else []
    history.append({"role": "user", "content": message})
    character_context = _build_character_context(list(characters), active_char)

    async def event_generator():
        t_start = time.monotonic()
        try:
            async for event in stream_chat(history, character_context, db):
                if event["type"] == "done":
                    # Save full conversation to DB
                    elapsed = round(time.monotonic() - t_start, 1)
                    final_messages = event["messages"]
                    if chat_session:
                        chat_session.messages = json.dumps(final_messages, default=str)
                        chat_session.updated_at = datetime.now(timezone.utc)
                    else:
                        db.add(ChatSession(
                            character_id=active_id,
                            messages=json.dumps(final_messages, default=str),
                        ))
                    await db.commit()
                    payload = {"type": "done", "stats": {**event["stats"], "elapsed_s": elapsed}}
                else:
                    payload = event
                yield f"data: {json.dumps(payload, default=str)}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/send", response_class=HTMLResponse)
async def send_message(
    request: Request,
    message: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    """Non-streaming fallback endpoint (kept for compatibility)."""
    active_id = request.session.get("active_character_id")
    if not active_id:
        return HTMLResponse("<p>Not authenticated.</p>", status_code=401)

    character_ids = request.session.get("character_ids", [])
    result = await db.execute(select(Character).where(Character.character_id.in_(character_ids)))
    characters = result.scalars().all()
    active_char = next((c for c in characters if c.character_id == active_id), None)

    if not active_char:
        return HTMLResponse("<p>Character not found.</p>", status_code=404)

    session_result = await db.execute(
        select(ChatSession).where(ChatSession.character_id == active_id)
        .order_by(ChatSession.updated_at.desc())
    )
    chat_session = session_result.scalar_one_or_none()
    history = json.loads(chat_session.messages) if chat_session else []

    history.append({"role": "user", "content": message})
    character_context = _build_character_context(list(characters), active_char)

    t_start = time.monotonic()
    response_text, updated_history, stats = await chat(history, character_context, db)
    elapsed = round(time.monotonic() - t_start, 1)

    if chat_session:
        chat_session.messages = json.dumps(updated_history, default=str)
        chat_session.updated_at = datetime.now(timezone.utc)
    else:
        db.add(ChatSession(
            character_id=active_id,
            messages=json.dumps(updated_history, default=str),
        ))
    await db.commit()

    return templates.TemplateResponse("partials/chat_messages.html", {
        "request": request,
        "new_messages": [
            {"role": "user", "text": message},
            {"role": "assistant", "text": response_text},
        ],
        "stats": {**stats, "elapsed_s": elapsed},
    })


@router.post("/clear")
async def clear_chat(request: Request, db: AsyncSession = Depends(get_db)):
    active_id = request.session.get("active_character_id")
    if active_id:
        result = await db.execute(select(ChatSession).where(ChatSession.character_id == active_id))
        sessions = result.scalars().all()
        for s in sessions:
            await db.delete(s)
        await db.commit()
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/chat", status_code=303)
