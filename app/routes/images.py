"""Image host tool — upload an image, convert to JPEG, share via short URL."""

import asyncio
import io
import secrets
import string
from datetime import datetime, timezone, timedelta
from pathlib import Path

from fastapi import APIRouter, Request, Depends, UploadFile, File, Form
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from PIL import Image, ImageOps, UnidentifiedImageError

from app.config import get_settings
from app.db.models import get_db, HostedImage

router = APIRouter(tags=["images"])
templates = Jinja2Templates(directory="app/templates")
settings = get_settings()

# ── Limits ────────────────────────────────────────────────────────────────────

MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB
MAX_DIMENSION = 4096                 # downscale longest side to this
JPEG_QUALITY = 85
# Largest canvas accepted, checked from the header before anything is decoded
# (ISS-169). An 8K screenshot is 33 MP and a full-resolution phone photo is
# about 49 MP; the output is cut to 4096 px on its longest side anyway. The
# 10 MB file cap does not bound this: a flat PNG under 1 MB can declare over
# 100 MP.
MAX_PIXELS = 50_000_000
# Pillow's own guard, as a backstop: it warns above this and refuses above
# twice it, while opening. The check in _convert_to_jpeg holds the exact limit;
# turning the warning into an error per call would need warnings.catch_warnings,
# which changes process-wide state and is not safe from the worker threads
# conversions run in.
Image.MAX_IMAGE_PIXELS = MAX_PIXELS
_TOO_LARGE = f"Image too large — the limit is {MAX_PIXELS // 1_000_000} megapixels."
# Formats whose header gives the size that gets decoded. Containers such as
# .ico pick an entry by its directory size and then decode it at whatever size
# it really is, which a header check cannot see (ISS-169).
ACCEPTED_FORMATS = ("PNG", "JPEG", "WEBP", "GIF", "BMP")
_UNSUPPORTED = "Could not read image — upload a PNG, JPEG, WebP, GIF or BMP file."

# Conversions run in a worker thread so the event loop keeps serving (ISS-169).
# A full-size decode and its copies come to hundreds of MB, so one runs at a
# time and a few may wait; past that an upload is refused, not queued.
CONVERT_CONCURRENCY = 1
CONVERT_QUEUE = 4

EXPIRY_OPTIONS = {
    "never":  None,
    "24h":    timedelta(hours=24),
    "1week":  timedelta(weeks=1),
    "1month": timedelta(days=30),
    "1year":  timedelta(days=365),
}

EXPIRY_LABELS = {
    "never":  "Never",
    "24h":    "24 hours",
    "1week":  "1 week",
    "1month": "1 month",
    "1year":  "1 year",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _uploads_path() -> Path:
    p = Path(settings.uploads_dir)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _file_path(image_id: str) -> Path:
    return _uploads_path() / f"{image_id}.jpg"


def _generate_id() -> str:
    chars = string.ascii_lowercase + string.digits
    return ''.join(secrets.choice(chars) for _ in range(8))


async def _unique_id(db: AsyncSession) -> str:
    """Generate a short ID that doesn't collide with an existing row."""
    for _ in range(10):
        candidate = _generate_id()
        existing = await db.execute(
            select(HostedImage.id).where(HostedImage.id == candidate)
        )
        if existing.scalar_one_or_none() is None:
            return candidate
    # Extremely unlikely — fall back to longer id
    return _generate_id() + _generate_id()[:4]


def _convert_to_jpeg(raw_bytes: bytes) -> tuple[bytes, int, int]:
    """Decode an arbitrary image, normalize, downscale if huge, return JPEG bytes + dims.

    Strips EXIF/metadata, flattens transparency over white, applies orientation tag.
    Raises ValueError on invalid input. Blocking: the upload route runs it
    through _convert_off_loop.
    """
    try:
        img = Image.open(io.BytesIO(raw_bytes), formats=ACCEPTED_FORMATS)
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        # The warning only arrives here where warnings are raised as errors.
        raise ValueError(_TOO_LARGE)
    except UnidentifiedImageError:
        raise ValueError(_UNSUPPORTED)
    except OSError as exc:
        raise ValueError(f"Could not read image: {exc}")
    # Opening reads only the header, so the size is known before decoding.
    if img.width * img.height > MAX_PIXELS:
        raise ValueError(_TOO_LARGE)
    # Only the current frame (the first) is decoded here or below, so an
    # animation's frame count costs nothing.
    try:
        img.load()
    except (Image.DecompressionBombError, OSError) as exc:
        raise ValueError(f"Could not read image: {exc}")
    # Again after decoding, before the full-size copies: no accepted format is
    # known to change size while decoding, but the copies would multiply it.
    if img.width * img.height > MAX_PIXELS:
        raise ValueError(_TOO_LARGE)

    # Honor EXIF rotation, then drop the metadata
    img = ImageOps.exif_transpose(img)

    # Flatten transparency onto white so JPEG looks correct
    if img.mode in ("RGBA", "LA"):
        bg = Image.new("RGB", img.size, (255, 255, 255))
        alpha = img.split()[-1]
        bg.paste(img.convert("RGB"), mask=alpha)
        img = bg
    elif img.mode == "P":
        img = img.convert("RGBA")
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        img = bg
    elif img.mode != "RGB":
        img = img.convert("RGB")

    # Downscale if huge (preserves aspect ratio)
    if max(img.size) > MAX_DIMENSION:
        img.thumbnail((MAX_DIMENSION, MAX_DIMENSION), Image.LANCZOS)

    out = io.BytesIO()
    img.save(out, format="JPEG", quality=JPEG_QUALITY, optimize=True, progressive=True)
    return out.getvalue(), img.width, img.height


class ConverterBusy(Exception):
    """Every conversion slot is taken and the queue is full."""


_convert_sem: asyncio.Semaphore | None = None
_convert_loop = None
_convert_waiting = 0


def _convert_gate() -> asyncio.Semaphore:
    """The conversion slots for the running loop. asyncio objects belong to
    one loop: production has one, each test client its own."""
    global _convert_sem, _convert_loop
    loop = asyncio.get_running_loop()
    if _convert_loop is not loop:
        _convert_sem, _convert_loop = asyncio.Semaphore(CONVERT_CONCURRENCY), loop
    return _convert_sem


async def _convert_off_loop(raw_bytes: bytes) -> tuple[bytes, int, int]:
    """_convert_to_jpeg in a worker thread, at most CONVERT_CONCURRENCY at once
    with CONVERT_QUEUE more waiting. Raises ConverterBusy past that."""
    global _convert_waiting
    gate = _convert_gate()
    if _convert_waiting >= CONVERT_QUEUE:
        raise ConverterBusy
    _convert_waiting += 1
    try:
        await gate.acquire()
    finally:
        _convert_waiting -= 1
    try:
        work = asyncio.get_running_loop().run_in_executor(None, _convert_to_jpeg, raw_bytes)
    except BaseException:
        gate.release()
        raise

    def _done(fut):
        gate.release()
        if not fut.cancelled():
            fut.exception()   # seen, even when its request has gone

    # The slot is held until the thread finishes, even if the request is
    # cancelled first: the thread can't be stopped, and its memory is what
    # the limit is for.
    work.add_done_callback(_done)
    return await asyncio.shield(work)


async def cleanup_expired_images(db: AsyncSession) -> int:
    """Delete expired image rows + their files. Returns count removed."""
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(HostedImage).where(
            HostedImage.expires_at.is_not(None),
            HostedImage.expires_at < now,
        )
    )
    expired = result.scalars().all()
    if not expired:
        return 0
    for img in expired:
        try:
            _file_path(img.id).unlink(missing_ok=True)
        except OSError:
            pass
        await db.delete(img)
    await db.commit()
    return len(expired)


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/tools/images", response_class=HTMLResponse)
async def images_upload_page(request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/")

    # Show user's recent uploads
    result = await db.execute(
        select(HostedImage)
        .where(HostedImage.user_id == user_id)
        .order_by(HostedImage.created_at.desc())
        .limit(50)
    )
    images = result.scalars().all()

    return templates.TemplateResponse(request, "tools_images.html", {"images": images,
        "max_mb": MAX_UPLOAD_BYTES // (1024 * 1024),
        "expiry_options": EXPIRY_LABELS})


@router.post("/tools/images/upload")
async def images_upload(
    request: Request,
    file: UploadFile = File(...),
    label: str = Form(""),
    expiry: str = Form("never"),
    db: AsyncSession = Depends(get_db),
):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/", status_code=303)

    async def _error(message: str, status: int):
        recent = await db.execute(
            select(HostedImage)
            .where(HostedImage.user_id == user_id)
            .order_by(HostedImage.created_at.desc())
            .limit(50)
        )
        return templates.TemplateResponse(request, "tools_images.html", {"error": message,
            "images": recent.scalars().all(),
            "max_mb": MAX_UPLOAD_BYTES // (1024 * 1024),
            "expiry_options": EXPIRY_LABELS,
        }, status_code=status)

    # Read file with size cap (defends against lying Content-Length)
    raw = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        return await _error(f"File too large — max {MAX_UPLOAD_BYTES // (1024*1024)} MB.", 413)

    if not raw:
        return await _error("Pick a file before uploading.", 400)

    try:
        jpeg_bytes, width, height = await _convert_off_loop(raw)
    except ConverterBusy:
        return await _error("Too many uploads are being converted — try again in a moment.", 503)
    except ValueError as exc:
        return await _error(str(exc), 400)

    image_id = await _unique_id(db)
    file_path = _file_path(image_id)
    file_path.write_bytes(jpeg_bytes)

    delta = EXPIRY_OPTIONS.get(expiry, None)
    expires_at = (datetime.now(timezone.utc) + delta) if delta else None

    record = HostedImage(
        id=image_id,
        user_id=user_id,
        label=(label.strip()[:128] or None),
        original_filename=(file.filename or None),
        width=width,
        height=height,
        size_bytes=len(jpeg_bytes),
        view_count=0,
        created_at=datetime.now(timezone.utc),
        expires_at=expires_at,
    )
    db.add(record)
    await db.commit()

    return RedirectResponse(f"/i/{image_id}", status_code=303)


@router.post("/tools/images/{image_id}/delete")
async def images_delete(image_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/", status_code=303)

    result = await db.execute(select(HostedImage).where(HostedImage.id == image_id))
    img = result.scalar_one_or_none()
    if not img or img.user_id != user_id:
        return RedirectResponse("/tools/images", status_code=303)

    try:
        _file_path(image_id).unlink(missing_ok=True)
    except OSError:
        pass
    await db.delete(img)
    await db.commit()
    return RedirectResponse("/tools/images", status_code=303)


# ── Public short URLs ─────────────────────────────────────────────────────────
# NOTE: the `.jpg` route must be declared BEFORE the bare {image_id} route,
# otherwise the path parameter would swallow ".jpg" as part of the id.

@router.get("/i/{image_id}.jpg")
async def image_raw(image_id: str, db: AsyncSession = Depends(get_db)):
    """Serve raw JPEG bytes. Public — no auth required."""
    result = await db.execute(select(HostedImage).where(HostedImage.id == image_id))
    img = result.scalar_one_or_none()
    if not img:
        return Response(status_code=404)

    if img.expires_at:
        expires = img.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires < datetime.now(timezone.utc):
            return Response(status_code=410)

    path = _file_path(image_id)
    if not path.exists():
        return Response(status_code=404)

    return FileResponse(
        path,
        media_type="image/jpeg",
        headers={
            "Cache-Control": "public, max-age=86400, immutable",
            "Content-Disposition": "inline",
        },
    )


@router.get("/i/{image_id}", response_class=HTMLResponse)
async def image_view(image_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    """View page for an image. Public — no auth required."""
    result = await db.execute(select(HostedImage).where(HostedImage.id == image_id))
    img = result.scalar_one_or_none()
    if not img:
        return templates.TemplateResponse(request, "tools_image_view.html", {
            "error": "Image not found or has expired.",
            "img": None,
        }, status_code=404)

    # Check expiry
    if img.expires_at:
        expires = img.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires < datetime.now(timezone.utc):
            try:
                _file_path(image_id).unlink(missing_ok=True)
            except OSError:
                pass
            await db.delete(img)
            await db.commit()
            return templates.TemplateResponse(request, "tools_image_view.html", {
                "error": "This image has expired.",
                "img": None,
            }, status_code=410)

    img.view_count = (img.view_count or 0) + 1
    await db.commit()

    is_owner = request.session.get("user_id") == img.user_id
    return templates.TemplateResponse(request, "tools_image_view.html", {
        "img": img,
        "is_owner": is_owner})
