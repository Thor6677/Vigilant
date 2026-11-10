"""Image uploads are size-checked before decoding and converted off the event loop (ISS-169).

POST /tools/images/upload decoded, copied, flattened, resized and re-encoded
the upload inside the async handler, so the single worker's event loop
served nothing else until it finished. The only bound was the 10 MB file
size, and a flat-colour PNG under 1 MB can declare well over 100 megapixels;
Pillow's guard warned from 80 MP and refused only past 160 MP.

The images here are built in memory: a real PNG for the happy path, and a
PNG header that declares a large canvas over almost no data for the size
check, so no test decodes anything big.
"""
import asyncio
import base64
import io
import json
import struct
import tempfile
import threading
import time
import zlib

import httpx
import itsdangerous
import pytest
from PIL import Image, ImageFile
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base, User, get_db
from app.routes import images as images_mod

USER_ID = 9441
_CSRF = "test-csrf-token-images-0123456789"


def _png_header(width, height):
    """A PNG whose header declares width x height over a few bytes of data."""
    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(b"\x00" * 64)) + chunk(b"IEND", b""))


def _png(width=64, height=48, mode="RGBA"):
    buf = io.BytesIO()
    Image.new(mode, (width, height), (200, 30, 30, 128)).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def decodes(monkeypatch):
    """Records every full decode Pillow starts."""
    calls = []
    real = ImageFile.ImageFile.load

    def _load(self):
        calls.append(self.size)
        return real(self)

    monkeypatch.setattr(ImageFile.ImageFile, "load", _load)
    return calls


# ── The size check ──────────────────────────────────────────────────────────

def test_an_image_over_the_pixel_limit_is_refused_before_decoding(decodes):
    raw = _png_header(7000, 7000)            # 49 MP, in under 200 bytes
    with pytest.raises(ValueError, match="megapixels"):
        images_mod._convert_to_jpeg(raw)
    assert decodes == []


def test_the_pixel_limit_suits_screenshots():
    assert 7680 * 4320 <= images_mod.MAX_PIXELS < 7000 * 7000   # 8K fits; 49 MP does not


def test_an_image_far_over_the_limit_gets_the_same_message(decodes):
    with pytest.raises(ValueError, match="megapixels"):
        images_mod._convert_to_jpeg(_png_header(20000, 20000))
    assert decodes == []


def test_a_normal_image_still_converts():
    jpeg, width, height = images_mod._convert_to_jpeg(_png())
    assert (width, height) == (64, 48)
    assert Image.open(io.BytesIO(jpeg)).format == "JPEG"


# ── The route ───────────────────────────────────────────────────────────────

def _run_async(fn):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(fn())
    finally:
        loop.close()
        asyncio.set_event_loop(None)


@pytest.fixture
def app_env(monkeypatch, tmp_path):
    import app.main as main

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

    async def _setup():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with SessionLocal() as db:
            db.add(User(id=USER_ID))
            await db.commit()

    _run_async(_setup)

    async def override_get_db():
        async with SessionLocal() as session:
            yield session

    monkeypatch.setattr(images_mod, "_uploads_path", lambda: tmp_path)
    # Module-level converter state; raising=False so the test reaches its
    # assertions on code that has none of it.
    monkeypatch.setattr(images_mod, "_convert_loop", None, raising=False)
    monkeypatch.setattr(images_mod, "_convert_waiting", 0, raising=False)
    main.app.dependency_overrides[get_db] = override_get_db
    try:
        yield main.app
    finally:
        main.app.dependency_overrides.pop(get_db, None)

        async def _dispose():
            await engine.dispose()
        _run_async(_dispose)


def _client(app):
    import app.main as main
    signer = itsdangerous.TimestampSigner(main.settings.secret_key)
    payload = {"user_id": USER_ID, "csrf_token": _CSRF}
    cookie = signer.sign(base64.b64encode(json.dumps(payload).encode())).decode()
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                               base_url="https://testserver")
    client.cookies.set("vigilant_session", cookie)
    return client


def _upload(client):
    return client.post("/tools/images/upload", files={"file": ("a.png", _png(), "image/png")},
                       data={"label": "", "expiry": "24h"}, headers={"X-CSRF-Token": _CSRF})


class _SlowConverter:
    """Stands in for _convert_to_jpeg and holds each call until released."""

    def __init__(self):
        self.release = threading.Event()
        self.entered = threading.Event()
        self.lock = threading.Lock()
        self.running = 0
        self.max_running = 0
        self.returned_at = None

    def __call__(self, raw):
        with self.lock:
            self.running += 1
            self.max_running = max(self.max_running, self.running)
        self.entered.set()
        self.release.wait(timeout=3)
        with self.lock:
            self.running -= 1
        self.returned_at = time.monotonic()
        return b"\xff\xd8\xff\xd9", 1, 1


def test_the_event_loop_keeps_serving_while_an_upload_converts(app_env, monkeypatch):
    slow = _SlowConverter()
    monkeypatch.setattr(images_mod, "_convert_to_jpeg", slow)

    async def _go():
        async with _client(app_env) as client:
            upload = asyncio.ensure_future(_upload(client))
            await asyncio.to_thread(slow.entered.wait, 5)
            health = await client.get("/healthz")
            answered_at = time.monotonic()
            slow.release.set()
            return await upload, health, answered_at

    upload, health, answered_at = asyncio.run(_go())
    assert health.status_code == 200
    assert slow.returned_at is not None and answered_at < slow.returned_at, (
        "/healthz waited for the conversion to finish")
    assert upload.status_code == 303 and upload.headers["location"].startswith("/i/")


def test_uploads_past_the_queue_are_refused_not_piled_up(app_env, monkeypatch):
    slow = _SlowConverter()
    monkeypatch.setattr(images_mod, "_convert_to_jpeg", slow)
    accepted = images_mod.CONVERT_CONCURRENCY + images_mod.CONVERT_QUEUE
    extra = 2

    async def _go():
        async with _client(app_env) as client:
            tasks = [asyncio.ensure_future(_upload(client)) for _ in range(accepted + extra)]
            await asyncio.to_thread(slow.entered.wait, 5)
            # No slot frees until the release below, so the requests that
            # answer meanwhile are the refused ones.
            deadline = time.monotonic() + 3
            while sum(t.done() for t in tasks) < extra and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.1)
            early = [t.result().status_code for t in tasks if t.done()]
            slow.release.set()
            return early, [r.status_code for r in await asyncio.gather(*tasks)]

    early, statuses = asyncio.run(_go())
    assert early == [503] * extra, f"answered before any conversion finished: {early}"
    assert sorted(statuses) == [303] * accepted + [503] * extra
    assert slow.max_running == images_mod.CONVERT_CONCURRENCY


def test_an_upload_is_still_converted_and_stored(app_env, tmp_path):
    async def _go():
        async with _client(app_env) as client:
            return await _upload(client)

    r = asyncio.run(_go())
    assert r.status_code == 303 and r.headers["location"].startswith("/i/")
    (stored,) = tmp_path.glob("*.jpg")
    assert Image.open(stored).size == (64, 48)
