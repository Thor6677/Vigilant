"""SQLite INDEXED BY hint support (process-wide SQLAlchemy patch).

SQLAlchemy's `with_hint(..., dialect_name="sqlite")` compiles to a no-op on
stock SQLAlchemy: SQLiteCompiler doesn't override `get_from_hint_text` (only
the MySQL/Oracle/MSSQL/Sybase dialects do), so the base class's `None`
return silently drops the hint text with no error. Verified empirically:
`str(select(Killmail).with_hint(...).compile(dialect=sqlite.dialect()))`
produces a bare `FROM killmails` with no hint text.

Patch in the same mechanism those other dialects use — return the hint
text verbatim — so `INDEXED BY ix_killmails_killmail_time` actually reaches
the generated SQL. This is process-wide but inert unless a statement in
this codebase calls `.with_hint(dialect_name="sqlite")` (today only the
killfeed advanced search in app/routes/intel_kills_search.py).

Importing this module applies the patch and runs a self-check probe; import
it for its side effect:

    from app.db import sqlite_hints  # noqa: F401
"""

from sqlalchemy import column as _probe_col
from sqlalchemy import select as _probe_select
from sqlalchemy import table as _probe_table
from sqlalchemy.dialects.sqlite import dialect as _sqlite_dialect
from sqlalchemy.dialects.sqlite.base import SQLiteCompiler


def _sqlite_get_from_hint_text(self, table, text):  # pragma: no cover - trivial passthrough
    return text


SQLiteCompiler.get_from_hint_text = _sqlite_get_from_hint_text

# Self-check: with_hint relies on the (undocumented) get_from_hint_text hook.
# If a future SQLAlchemy renames/removes it, the assignment above still
# "succeeds" and hints silently vanish — fail startup loudly instead.
_probe_t = _probe_table("_hint_probe", _probe_col("x"))
_probe_sql = str(
    _probe_select(_probe_t.c.x)
    .with_hint(_probe_t, "INDEXED BY __probe__", dialect_name="sqlite")
    .compile(dialect=_sqlite_dialect())
)
if "INDEXED BY __probe__" not in _probe_sql:
    raise RuntimeError(
        "SQLite INDEXED BY monkeypatch no longer effective "
        "(SQLAlchemy get_from_hint_text hook changed?) — see app/db/sqlite_hints.py"
    )
del _probe_t, _probe_sql
