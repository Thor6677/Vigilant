import os
import tempfile

os.environ.setdefault("EVE_CLIENT_ID", "test")
os.environ.setdefault("EVE_CLIENT_SECRET", "test")
os.environ.setdefault("SECRET_KEY", "test-secret")

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

    from app.db.models import Base, engine
    import app.db.sde_models  # noqa: F401 — registers the sde_* tables on Base

    async def _create_schema():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        # Release pooled connections: every test drives its own fresh event
        # loop, and a connection pinned to this one would be unusable there.
        await engine.dispose()

    asyncio.run(_create_schema())
