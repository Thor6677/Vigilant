import glob
import os
import shutil
import tempfile
import time

os.environ.setdefault("EVE_CLIENT_ID", "test")
os.environ.setdefault("EVE_CLIENT_SECRET", "test")
os.environ.setdefault("SECRET_KEY", "test-secret")

# Every temp file the suite creates lands in one directory per run, removed in
# pytest_unconfigure. Most fixtures build their database with
# NamedTemporaryFile(delete=False) and never delete it; each is a full-schema
# SQLite file of about 1 MB, so routine runs left tens of GB in the system temp
# dir. Redirecting tempfile here covers every one of those call sites, their
# -wal/-shm files, and any new test written the same way. pytest's own tmp_path
# root resolves through tempfile too, so it is removed with the run as well.
#
# Only Python's tempfile is redirected. TMPDIR is left alone: the updater
# resolves `${TMPDIR:-/tmp}` itself and its tests steer that directly.
#
# A run that is killed outright (SIGKILL, closed terminal) never reaches
# pytest_unconfigure, so sweep run dirs left behind by earlier runs. The
# day-old cutoff leaves a run going on at the same time in another checkout
# alone.
_RUN_PREFIX = "vigilant-pytest-"
for _stale in glob.glob(os.path.join(tempfile.gettempdir(), _RUN_PREFIX + "*")):
    try:
        if time.time() - os.path.getmtime(_stale) > 24 * 3600:
            shutil.rmtree(_stale, ignore_errors=True)
    except OSError:
        pass
_RUN_TMP = tempfile.mkdtemp(prefix=_RUN_PREFIX)
tempfile.tempdir = _RUN_TMP

# The route smoke tests drive a TestClient against the real `app.main`, whose
# engine is built from DATABASE_URL at import time (app/db/models.py:10). Left
# alone that resolves to the default ./vigilant.db, so the suite would read and
# write the developer's own database — passing locally only because that file
# happens to exist with a live schema, and failing on a fresh clone or in CI
# with "no such table: characters".
#
# Point the app at a throwaway file instead and create the schema up front, so
# the suite is hermetic and behaves identically everywhere. Set unconditionally
# rather than via setdefault: a real DATABASE_URL inherited from the shell or a
# local .env is never something a test run should be allowed to touch.
_TEST_DB = os.path.join(tempfile.mkdtemp(prefix="vigilant-tests-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TEST_DB}"


def pytest_configure(config):
    """Create the full schema in the throwaway DB before any test runs.

    Tests that need fixture data still build their own temp-file engine (see
    `_temp_engine` in test_networth.py); this only guarantees that routes
    reached through TestClient find tables rather than an empty file.
    """
    import asyncio

    from app.db.models import Base, engine, ensure_wal
    import app.db.sde_models  # noqa: F401 — registers the sde_* tables on Base

    async def _create_schema():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        # Release pooled connections: every test drives its own fresh event
        # loop, and a connection pinned to this one would be unusable there.
        await engine.dispose()

    # ISS-075: the engine hook no longer sets WAL, so do it here like startup.
    ensure_wal()
    asyncio.run(_create_schema())


def pytest_unconfigure(config):
    """Remove this run's temp dir. Runs on Ctrl-C as well as a normal finish."""
    shutil.rmtree(_RUN_TMP, ignore_errors=True)


def ensure_user(user_id: int, db_url: str | None = None) -> int:
    """Make sure a users row with this id exists, and return the id.

    Every request whose session names a user_id is checked against that user's
    row (app/auth/session_guard.py), so a forged test cookie must name a user
    that exists in the database the request will read. By default that is the
    hermetic DB above; a test that overrides get_db with its own engine passes
    that engine's URL. The row is left with no session_epoch, which matches a
    cookie that carries none — reset each time, because a test that runs the
    app's startup (`with TestClient(...)`) assigns every existing user one.
    """
    import asyncio

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    async def _insert():
        eng = create_async_engine(db_url or os.environ["DATABASE_URL"])
        try:
            async with eng.begin() as conn:
                await conn.execute(text("INSERT OR IGNORE INTO users (id, role) VALUES (:id, 'user')"),
                                   {"id": user_id})
                await conn.execute(text("UPDATE users SET session_epoch = NULL WHERE id = :id"),
                                   {"id": user_id})
        finally:
            await eng.dispose()

    asyncio.run(_insert())
    return user_id
