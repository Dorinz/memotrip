#!/usr/bin/env python3
"""db.py — dual-backend database access for MemoTrip.

Local dev (DATABASE_URL unset): sqlite3 against trips.db in the repo root,
behavior unchanged from the original inline webapp.py code — including the
ad-hoc PRAGMA/ALTER TABLE migration dance, since local .db files may be
mid-evolution and must keep working.

Production (DATABASE_URL set): Postgres via psycopg2, with a RealDictCursor
so `row["col"]` access works identically to sqlite3.Row. Schema is the clean
final shape (SERIAL instead of AUTOINCREMENT, no migration dance) since a
fresh prod database starts empty.

Callers keep writing SQL with `?` placeholders exactly as before; _q()
rewrites them to psycopg2's `%s` on the Postgres path only.
"""
from __future__ import annotations

import pathlib
import sqlite3

import config

ROOT = pathlib.Path(__file__).resolve().parent
DB = ROOT / "trips.db"          # sqlite path — unused when DATABASE_URL is set

_IS_PG = bool(config.DATABASE_URL)


def _q(sql: str) -> str:
    """Rewrite '?' placeholders to psycopg2's '%s'. This is a plain
    substitution, not placeholder-aware parsing — it is only safe because
    none of this codebase's SQL strings contain a literal '?' character
    (verified by inspection of every call site that uses db()/execute() as
    of this writing). If a future query needs a literal '?' in a string
    literal, it will need to avoid this helper."""
    return sql.replace("?", "%s") if _IS_PG else sql


class _PGCursorResult:
    """Thin shim so `c.execute(sql, params)` returns something offering
    .fetchone()/.fetchall(), matching the sqlite3.Connection.execute(...)
    shorthand used throughout webapp.py."""

    def __init__(self, cur):
        self._cur = cur

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()


class _PGConn:
    """Wraps a psycopg2 connection so it supports the same idioms the
    existing code already uses on a raw sqlite3.Connection:
      - `with db() as c: c.execute(...)`     (commit on success, rollback on
        exception — matching sqlite3's own context-manager behavior)
      - `c.execute(sql, params).fetchone()`  (cursor-shorthand chaining)
    It additionally closes the underlying connection on __exit__ — sqlite3
    does not auto-close on `with`, but nothing in this codebase keeps a
    connection alive past its `with` block, so closing here avoids leaking
    Postgres connections without changing any observable behavior.
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


def db():
    """A connection-like object: sqlite3.Connection (dev) or a _PGConn
    wrapper (prod), depending on whether DATABASE_URL is set."""
    if _IS_PG:
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(config.DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
        return _PGConn(conn)
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def init_schema() -> None:
    if _IS_PG:
        _init_schema_pg()
    else:
        _init_schema_sqlite()


def _init_schema_sqlite() -> None:
    """Verbatim copy of the original webapp.py schema/migration block."""
    with db() as _c:
        _c.execute("""CREATE TABLE IF NOT EXISTS trips(
            id TEXT PRIMARY KEY, created TEXT, description TEXT, region_hint TEXT,
            status TEXT, stage TEXT, log TEXT, error TEXT,
            picker_uri TEXT, picker_sid TEXT)""")
        # user_id: nullable - a guest's trip has none, and is never listed under any account
        _cols = {r["name"] for r in _c.execute("PRAGMA table_info(trips)")}
        if "user_id" not in _cols:
            _c.execute("ALTER TABLE trips ADD COLUMN user_id INTEGER")
        _c.execute("""CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL, display_name TEXT, created TEXT)""")
        _ucols = {r["name"] for r in _c.execute("PRAGMA table_info(users)")}
        if "email" not in _ucols and "username" in _ucols:
            _c.execute("ALTER TABLE users RENAME COLUMN username TO email")
            _ucols.discard("username"); _ucols.add("email")
        if "display_name" not in _ucols:
            _c.execute("ALTER TABLE users ADD COLUMN display_name TEXT")
        # per-user Google Photos OAuth tokens (one connection per user)
        _c.execute("""CREATE TABLE IF NOT EXISTS photo_accounts(
            user_id INTEGER PRIMARY KEY,
            refresh_token TEXT, access_token TEXT, token_expiry TEXT, granted TEXT)""")


def _init_schema_pg() -> None:
    """Clean final schema — prod starts empty, so no migration dance needed."""
    with db() as _c:
        _c.execute("""CREATE TABLE IF NOT EXISTS users(
            id SERIAL PRIMARY KEY, email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL, display_name TEXT, created TEXT)""")
        _c.execute("""CREATE TABLE IF NOT EXISTS trips(
            id TEXT PRIMARY KEY, created TEXT, description TEXT, region_hint TEXT,
            status TEXT, stage TEXT, log TEXT, error TEXT,
            picker_uri TEXT, picker_sid TEXT, user_id INTEGER)""")
        _c.execute("""CREATE TABLE IF NOT EXISTS photo_accounts(
            user_id INTEGER PRIMARY KEY REFERENCES users(id),
            refresh_token TEXT, access_token TEXT, token_expiry TEXT, granted TEXT)""")
