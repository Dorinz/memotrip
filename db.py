#!/usr/bin/env python3
"""db.py — database access for MemoTrip: Postgres everywhere, via psycopg2.

DATABASE_URL picks the database: Cloud SQL in prod, the Docker container
from docker-compose.yml in local dev (see .env), its memotrip_test database
in tests (see tests/conftest.py). A RealDictCursor gives `row["col"]` access.

Callers write SQL with `?` placeholders; _q() rewrites them to psycopg2's `%s`.
The schema itself lives in Alembic migrations (migrations/versions/).
"""

from __future__ import annotations

import datetime as dt
import pathlib

import config

ROOT = pathlib.Path(__file__).resolve().parent


def now_utc() -> dt.datetime:
    """Every timestamp column is TIMESTAMPTZ, always written as UTC."""
    return dt.datetime.now(dt.timezone.utc)


def as_utc(value: dt.datetime | None) -> dt.datetime | None:
    """A TIMESTAMPTZ value -> the same moment as an aware UTC datetime
    (psycopg2 returns it in the connection's session time zone)."""
    if value is None:
        return None
    return value.astimezone(dt.timezone.utc)


def _q(sql: str) -> str:
    """Rewrite '?' placeholders to psycopg2's '%s'. This is a plain
    substitution, not placeholder-aware parsing — it is only safe because
    none of this codebase's SQL strings contain a literal '?' character
    (verified by inspection of every call site that uses db()/execute() as
    of this writing). If a future query needs a literal '?' in a string
    literal, it will need to avoid this helper."""
    return sql.replace("?", "%s")


class _PGCursorResult:
    """Thin shim so `c.execute(sql, params)` returns something offering
    .fetchone()/.fetchall(), the sqlite3-style shorthand used throughout
    webapp.py."""

    def __init__(self, cur):
        self._cur = cur

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    def __iter__(self):
        return iter(self._cur)


class _PGConn:
    """Wraps a psycopg2 connection so it supports the idioms the code uses:
      - `with db() as c: c.execute(...)`     (commit on success, rollback on
        exception)
      - `c.execute(sql, params).fetchone()`  (cursor-shorthand chaining)
    It also closes the underlying connection on __exit__ — nothing in this
    codebase keeps a connection alive past its `with` block.
    """

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=()):
        cur = self._conn.cursor()
        cur.execute(_q(sql), params)
        return _PGCursorResult(cur)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self._conn.commit()
            else:
                self._conn.rollback()
        finally:
            self._conn.close()
        return False


def db() -> _PGConn:
    if not config.DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL is not set. For local dev, start the database with "
            "`docker compose up -d` and set DATABASE_URL in .env (see BUILD_README.md)."
        )
    import psycopg2
    import psycopg2.extras

    conn = psycopg2.connect(config.DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    return _PGConn(conn)


def init_schema() -> None:
    """Bring the database at DATABASE_URL up to the latest schema: Alembic
    runs every migration in migrations/versions/ it hasn't run there yet
    (it records them in the alembic_version table). Called on every startup,
    so a schema change - a new migration file - reaches each database (dev,
    then prod on deploy) exactly once, with nothing to run by hand."""
    from alembic import command
    from alembic.config import Config

    command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
