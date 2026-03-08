import json
import time
from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import HTMLResponse, StreamingResponse, RedirectResponse
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


def _make_title(message: str) -> str:
    """Generate a short title from the first user message."""
    title = message.strip().split("\n")[0]
    return title[:48] + "…" if len(title) > 48 else title


async def _get_active_session(request: Request, active_id: int, db: AsyncSession):
    """Return the current ChatSession based on session cookie, or None.
    Returns None both when no session is set AND when 'new' sentinel is set."""
    session_id = request.session.get("active_chat_session_id")
    if not session_id or session_id == "new":
        return None
    result = await db.execute(
        select(ChatSession).where(
            ChatSession.id == session_id,
            ChatSession.character_id == active_id,
        )
    )
    return result.scalar_one_or_none()


@router.get("", response_class=HTMLResponse)
async def chat_page(request: Request, db: AsyncSession = Depends(get_db)):
    active_id = request.session.get("active_character_id")
    if not active_id:
        return RedirectResponse("/")

    character_ids = request.session.get("character_ids", [])
    result = await db.execute(select(Character).where(Character.character_id.in_(character_ids)))
    characters = result.scalars().all()
    active_char = next((c for c in characters if c.character_id == active_id), None)

    # Load all sessions for this character (for history sidebar)
    sessions_result = await db.execute(
        select(ChatSession)
        .where(ChatSession.character_id == active_id)
        .order_by(ChatSession.updated_at.desc())
    )
    all_sessions = sessions_result.scalars().all()

    # Active session — only auto-select the most recent if no explicit choice has been made yet
    cookie_val = request.session.get("active_chat_session_id")
    chat_session = await _get_active_session(request, active_id, db)
    if not chat_session and cookie_val is None and all_sessions:
        # First ever visit: load the most recent session
        chat_session = all_sessions[0]
        request.session["active_chat_session_id"] = chat_session.id

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
        "all_sessions": all_sessions,
        "active_session_id": chat_session.id if chat_session else None,
        "llm_model": _active_model(),
        "llm_provider": settings.llm_provider,
    })


@router.post("/new")
async def new_chat(request: Request):
    """Start a fresh chat session."""
    request.session["active_chat_session_id"] = "new"
    return RedirectResponse("/chat", status_code=303)


@router.post("/session/{session_id}")
async def select_session(session_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    """Switch to a previous chat session."""
    active_id = request.session.get("active_character_id")
    result = await db.execute(
        select(ChatSession).where(
            ChatSession.id == session_id,
            ChatSession.character_id == active_id,
        )
    )
    if result.scalar_one_or_none():
        request.session["active_chat_session_id"] = session_id
    return RedirectResponse("/chat", status_code=303)


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

    chat_session = await _get_active_session(request, active_id, db)
    history = json.loads(chat_session.messages) if chat_session else []
    is_first_message = len(history) == 0

    history.append({"role": "user", "content": message})
    character_context = _build_character_context(list(characters), active_char)

    # Capture session_id for use inside the generator (avoids session access in async gen)
    current_session_id = chat_session.id if chat_session else None
    title = _make_title(message) if is_first_message else (chat_session.title if chat_session else "New Chat")

    async def event_generator():
        nonlocal current_session_id
        t_start = time.monotonic()
        try:
            async for event in stream_chat(history, character_context, db):
                if event["type"] == "done":
                    elapsed = round(time.monotonic() - t_start, 1)
                    final_messages = event["messages"]
                    if current_session_id:
                        upd = await db.execute(select(ChatSession).where(ChatSession.id == current_session_id))
                        sess = upd.scalar_one_or_none()
                        if sess:
                            sess.messages = json.dumps(final_messages, default=str)
                            sess.updated_at = datetime.now(timezone.utc)
                    else:
                        new_sess = ChatSession(
                            character_id=active_id,
                            title=title,
                            messages=json.dumps(final_messages, default=str),
                        )
                        db.add(new_sess)
                        await db.flush()
                        current_session_id = new_sess.id
                        request.session["active_chat_session_id"] = new_sess.id
                    await db.commit()
                    payload = {
                        "type": "done",
                        "stats": {**event["stats"], "elapsed_s": elapsed},
                        "session": {"id": current_session_id, "title": title, "is_new": is_first_message},
                    }
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


@router.post("/clear")
async def clear_chat(request: Request, db: AsyncSession = Depends(get_db)):
    """Delete the current active chat session only."""
    active_id = request.session.get("active_character_id")
    session_id = request.session.get("active_chat_session_id")
    if active_id and session_id:
        result = await db.execute(
            select(ChatSession).where(
                ChatSession.id == session_id,
                ChatSession.character_id == active_id,
            )
        )
        sess = result.scalar_one_or_none()
        if sess:
            await db.delete(sess)
            await db.commit()
    request.session.pop("active_chat_session_id", None)
    return RedirectResponse("/chat", status_code=303)
