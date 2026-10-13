#!/usr/bin/env python3
"""db.py — database access for MemoTrip: Postgres everywhere, via psycopg2.

DATABASE_URL picks the database: Cloud SQL in prod, the Docker container
from docker-compose.yml in local dev (see .env), its memotrip_test database
in tests (see tests/conftest.py). A RealDictCursor gives `row["col"]` access.

Callers write SQL with `?` placeholders; _q() rewrites them to psycopg2's `%s`.
"""

from __future__ import annotations

import datetime as dt

import config


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
    """Creates any missing table, index or column - safe to rerun on every
    startup, and the one place a schema change is written. A new column goes
    in its CREATE TABLE (for a fresh database) AND as an
    `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` below it (for an existing one)."""
    with db() as _c:
        # several Cloud Run instances can start at once - only one runs this
        # at a time (the lock is released at commit)
        _c.execute("SELECT pg_advisory_xact_lock(4243001)")
        _c.execute("""CREATE TABLE IF NOT EXISTS users(
            id SERIAL PRIMARY KEY, email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL, display_name TEXT, created TIMESTAMPTZ)""")
        # user_id: nullable - a guest's trip has none, and is never listed
        # under any account; deleting a user deletes their trips' rows
        _c.execute("""CREATE TABLE IF NOT EXISTS trips(
            id TEXT PRIMARY KEY, created TIMESTAMPTZ, description TEXT, region_hint TEXT,
            status TEXT, stage TEXT, error TEXT, picker_uri TEXT, picker_sid TEXT,
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE, photo_owner_key TEXT)""")
        # one row per logline() call - see webapp.logline()
        _c.execute("""CREATE TABLE IF NOT EXISTS trip_logs(
            id BIGSERIAL PRIMARY KEY,
            trip_id TEXT NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
            ts TIMESTAMPTZ NOT NULL, msg TEXT NOT NULL)""")
        _c.execute("CREATE INDEX IF NOT EXISTS idx_trip_logs_trip ON trip_logs(trip_id, id)")
        # Google Photos OAuth tokens, keyed by owner_key - "user:<id>" for a
        # logged-in account or "guest:<random>" for an anonymous session (see
        # webapp.photo_owner_key), not a users.id FK, so a guest can connect
        # their own Google Photos without ever creating a MemoTrip account.
        _c.execute("""CREATE TABLE IF NOT EXISTS photo_accounts(
            owner_key TEXT PRIMARY KEY,
            refresh_token TEXT, access_token TEXT, token_expiry TEXT, granted TEXT)""")
        # one row per Gemini-backed generation action (new trip / rerun / photo
        # pick finish), keyed by the same identity as photo_owner_key, plus the
        # client IP for a second, per-network daily cap (a guest identity
        # resets by just clearing cookies) - see webapp.quota_exceeded().
        _c.execute("""CREATE TABLE IF NOT EXISTS generations(
            id SERIAL PRIMARY KEY, identity TEXT NOT NULL, ts TIMESTAMPTZ NOT NULL, ip TEXT)""")
        _c.execute(
            "CREATE INDEX IF NOT EXISTS idx_generations_identity_ts ON generations(identity, ts)"
        )
        _c.execute("CREATE INDEX IF NOT EXISTS idx_generations_ip_ts ON generations(ip, ts)")
