#!/usr/bin/env python3
"""db.py — dual-backend database access for MemoTrip.

Local dev (DATABASE_URL unset): sqlite3 against trips.db in the repo root,
including an ad-hoc PRAGMA/ALTER TABLE migration dance, since local .db files
may be mid-evolution and must keep working.

Production (DATABASE_URL set): Postgres via psycopg2, with a RealDictCursor
so `row["col"]` access works identically to sqlite3.Row. Its schema code
also migrates the live database in place (SERIAL instead of AUTOINCREMENT,
TIMESTAMPTZ instead of TEXT timestamps).

Callers keep writing SQL with `?` placeholders exactly as before; _q()
rewrites them to psycopg2's `%s` on the Postgres path only.
"""

from __future__ import annotations

import datetime as dt
import pathlib
import sqlite3

import config

ROOT = pathlib.Path(__file__).resolve().parent
DB = ROOT / "trips.db"  # sqlite path — unused when DATABASE_URL is set

_IS_PG = bool(config.DATABASE_URL)

# Timestamps (trips.created, users.created, generations.ts, trip_logs.ts) are
# always UTC. Postgres stores them as TIMESTAMPTZ; sqlite has no date type, so
# they're TEXT in this one fixed UTC format - which also sorts and compares
# correctly as plain strings.
SQLITE_TS_FMT = "%Y-%m-%d %H:%M:%S"


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _sqlite_ts(value: dt.datetime) -> str:
    """sqlite3 adapter: a datetime -> UTC text in SQLITE_TS_FMT (a naive one
    is taken to already be UTC), so callers can pass datetimes as query
    params on both backends."""
    if value.tzinfo is not None:
        value = value.astimezone(dt.timezone.utc)
    return value.strftime(SQLITE_TS_FMT)


sqlite3.register_adapter(dt.datetime, _sqlite_ts)


def as_utc(value) -> dt.datetime | None:
    """A timestamp column's value -> an aware UTC datetime, whichever backend
    it came from (a datetime from Postgres, SQLITE_TS_FMT text from sqlite).
    None for an empty or unparseable value."""
    if value is None or value == "":
        return None
    if isinstance(value, str):
        try:
            value = dt.datetime.fromisoformat(value)
        except ValueError:
            return None
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


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


def _sqlite_connect() -> sqlite3.Connection:
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def db():
    """A connection-like object: sqlite3.Connection (dev) or a _PGConn
    wrapper (prod), depending on whether DATABASE_URL is set."""
    if _IS_PG:
        import psycopg2
        import psycopg2.extras

        conn = psycopg2.connect(config.DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
        return _PGConn(conn)
    c = _sqlite_connect()
    # sqlite only enforces REFERENCES clauses when this is on, per connection
    c.execute("PRAGMA foreign_keys = ON")
    return c


def init_schema() -> None:
    if _IS_PG:
        _init_schema_pg()
    else:
        _init_schema_sqlite()


# trips' final sqlite shape, shared by the fresh-create and the rebuild paths
# below. user_id is nullable - a guest's trip has none, and is never listed
# under any account; deleting a user deletes their trips' rows.
_SQLITE_TRIPS_DDL = """CREATE TABLE {name}(
    id TEXT PRIMARY KEY, created TEXT, description TEXT, region_hint TEXT,
    status TEXT, stage TEXT, error TEXT, picker_uri TEXT, picker_sid TEXT,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE, photo_owner_key TEXT)"""
_TRIPS_COLS = (
    "id, created, description, region_hint, status, stage, error, "
    "picker_uri, picker_sid, user_id, photo_owner_key"
)


def _sqlite_localtime_to_utc(c, table: str, col: str) -> None:
    """One-time fix for rows written before timestamps were UTC: those were
    time.strftime("%Y-%m-%d %H:%M") in this machine's local time - exactly 16
    chars, which no SQLITE_TS_FMT value is, so this is safe to rerun."""
    rows = c.execute(
        f"SELECT rowid AS rid, {col} AS v FROM {table} WHERE length({col}) = 16"
    ).fetchall()
    for r in rows:
        try:
            local = dt.datetime.strptime(r["v"], "%Y-%m-%d %H:%M").astimezone()
        except ValueError:
            continue
        c.execute(f"UPDATE {table} SET {col}=? WHERE rowid=?", (_sqlite_ts(local), r["rid"]))


def _init_schema_sqlite() -> None:
    """Creates the schema, and migrates an older local trips.db in place.
    Runs on a connection with foreign keys OFF (sqlite's default): the trips
    rebuild below drops and recreates trips, which with enforcement on would
    cascade-delete every trip_logs row."""
    with _sqlite_connect() as _c:
        _c.execute("""CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL, display_name TEXT, created TEXT)""")
        _ucols = {r["name"] for r in _c.execute("PRAGMA table_info(users)")}
        if "email" not in _ucols and "username" in _ucols:
            _c.execute("ALTER TABLE users RENAME COLUMN username TO email")
            _ucols.discard("username")
            _ucols.add("email")
        if "display_name" not in _ucols:
            _c.execute("ALTER TABLE users ADD COLUMN display_name TEXT")

        _cols = {r["name"] for r in _c.execute("PRAGMA table_info(trips)")}
        if not _cols:
            _c.execute(_SQLITE_TRIPS_DDL.format(name="trips"))
        # one row per logline() call - see webapp.logline()
        _c.execute("""CREATE TABLE IF NOT EXISTS trip_logs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trip_id TEXT NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
            ts TEXT NOT NULL, msg TEXT NOT NULL)""")
        _c.execute("CREATE INDEX IF NOT EXISTS idx_trip_logs_trip ON trip_logs(trip_id, id)")
        if _cols:
            if "user_id" not in _cols:
                _c.execute("ALTER TABLE trips ADD COLUMN user_id INTEGER")
            if "photo_owner_key" not in _cols:
                _c.execute("ALTER TABLE trips ADD COLUMN photo_owner_key TEXT")
            if "log" in _cols:
                # the log used to be one growing TEXT column on the trip row -
                # keep each old log as a single trip_logs entry (its lines
                # still come back joined exactly as before)
                _c.execute(
                    "INSERT INTO trip_logs(trip_id, ts, msg) "
                    "SELECT id, COALESCE(created, ''), rtrim(log, char(10)) FROM trips "
                    "WHERE log IS NOT NULL AND log <> ''"
                )
            _fks = {r["table"] for r in _c.execute("PRAGMA foreign_key_list(trips)")}
            if "log" in _cols or "users" not in _fks:
                # sqlite can't add a REFERENCES clause to an existing column,
                # so rebuild the table (which also drops log); a user_id
                # pointing at an already-deleted user becomes NULL so the new
                # constraint holds
                _c.execute(_SQLITE_TRIPS_DDL.format(name="trips_new"))
                _sel = _TRIPS_COLS.replace(
                    "user_id,", "CASE WHEN user_id IN (SELECT id FROM users) THEN user_id END,"
                )
                _c.execute(f"INSERT INTO trips_new({_TRIPS_COLS}) SELECT {_sel} FROM trips")
                _c.execute("DROP TABLE trips")
                _c.execute("ALTER TABLE trips_new RENAME TO trips")
        _sqlite_localtime_to_utc(_c, "trips", "created")
        _sqlite_localtime_to_utc(_c, "users", "created")
        _sqlite_localtime_to_utc(_c, "trip_logs", "ts")

        # Google Photos OAuth tokens, keyed by owner_key - "user:<id>" for a
        # logged-in account or "guest:<random>" for an anonymous session (see
        # webapp.photo_owner_key), so a guest can connect their own Google
        # Photos without ever creating a MemoTrip account. A table from
        # before this change gets dropped and recreated - it only ever held
        # reconnectable OAuth tokens, nothing worth migrating.
        _pcols = {r["name"] for r in _c.execute("PRAGMA table_info(photo_accounts)")}
        if _pcols and "owner_key" not in _pcols:
            _c.execute("DROP TABLE photo_accounts")
        _c.execute("""CREATE TABLE IF NOT EXISTS photo_accounts(
            owner_key TEXT PRIMARY KEY,
            refresh_token TEXT, access_token TEXT, token_expiry TEXT, granted TEXT)""")
        # one row per Gemini-backed generation action (new trip / rerun / photo
        # pick finish), keyed by the same identity as photo_owner_key - see
        # webapp.quota_exceeded(). ts is UTC, queried against a day window
        # computed in Israel time.
        _c.execute("""CREATE TABLE IF NOT EXISTS generations(
            id INTEGER PRIMARY KEY AUTOINCREMENT, identity TEXT NOT NULL, ts TEXT NOT NULL)""")
        _c.execute(
            "CREATE INDEX IF NOT EXISTS idx_generations_identity_ts ON generations(identity, ts)"
        )
        # the client IP of each generation - a second, per-network daily cap,
        # since a guest identity resets by just clearing cookies
        if "ip" not in {r["name"] for r in _c.execute("PRAGMA table_info(generations)")}:
            _c.execute("ALTER TABLE generations ADD COLUMN ip TEXT")
        _c.execute("CREATE INDEX IF NOT EXISTS idx_generations_ip_ts ON generations(ip, ts)")


def _pg_columns(c, table: str) -> dict[str, str]:
    """column name -> data_type for one of our tables."""
    return {
        r["column_name"]: r["data_type"]
        for r in c.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name=?",
            (table,),
        ).fetchall()
    }


def _pg_text_to_timestamptz(c, table: str, col: str) -> None:
    """One-time conversion of a timestamp column from TEXT to TIMESTAMPTZ.
    The old text was written in the server's clock, which on Cloud Run is
    UTC. Anything that doesn't start with a date becomes NULL rather than
    failing the whole migration."""
    if _pg_columns(c, table).get(col) != "text":
        return
    c.execute(
        f"ALTER TABLE {table} ALTER COLUMN {col} TYPE TIMESTAMPTZ USING "
        f"CASE WHEN {col} ~ '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}' "
        f"THEN {col}::timestamp AT TIME ZONE 'UTC' END"
    )


def _init_schema_pg() -> None:
    """Creates the schema, and brings an existing prod database up to it -
    each step checks the current shape first, so this is safe to rerun on
    every startup."""
    with db() as _c:
        # several Cloud Run instances can start at once - only one migrates at
        # a time (the lock is released at commit)
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
        _c.execute("""ALTER TABLE trips ADD COLUMN IF NOT EXISTS photo_owner_key TEXT""")
        _pg_text_to_timestamptz(_c, "users", "created")
        _pg_text_to_timestamptz(_c, "trips", "created")

        _has_fk = _c.execute(
            "SELECT 1 FROM pg_constraint WHERE conrelid = 'trips'::regclass "
            "AND contype = 'f' AND confrelid = 'users'::regclass"
        ).fetchone()
        if not _has_fk:
            # a trip whose user is already gone becomes unowned, so the new
            # constraint holds
            _c.execute(
                "UPDATE trips SET user_id = NULL WHERE user_id IS NOT NULL "
                "AND user_id NOT IN (SELECT id FROM users)"
            )
            _c.execute(
                "ALTER TABLE trips ADD CONSTRAINT trips_user_id_fkey FOREIGN KEY (user_id) "
                "REFERENCES users(id) ON DELETE CASCADE"
            )

        # one row per logline() call - see webapp.logline()
        _c.execute("""CREATE TABLE IF NOT EXISTS trip_logs(
            id BIGSERIAL PRIMARY KEY,
            trip_id TEXT NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
            ts TIMESTAMPTZ NOT NULL, msg TEXT NOT NULL)""")
        _c.execute("CREATE INDEX IF NOT EXISTS idx_trip_logs_trip ON trip_logs(trip_id, id)")
        if "log" in _pg_columns(_c, "trips"):
            # the log used to be one growing TEXT column on the trip row -
            # keep each old log as a single trip_logs entry (its lines still
            # come back joined exactly as before)
            _c.execute(
                "INSERT INTO trip_logs(trip_id, ts, msg) "
                "SELECT id, COALESCE(created, now()), rtrim(log, chr(10)) FROM trips "
                "WHERE log IS NOT NULL AND log <> ''"
            )
            _c.execute("ALTER TABLE trips DROP COLUMN log")

        # Was dropped-and-recreated once: a live deploy had created an earlier
        # version keyed by a plain user_id, before guests needed it too.
        # CREATE TABLE IF NOT EXISTS never alters an existing table, hence the
        # explicit check - it only ever held reconnectable OAuth tokens.
        _pcols = _pg_columns(_c, "photo_accounts")
        if _pcols and "owner_key" not in _pcols:
            _c.execute("DROP TABLE photo_accounts")
        # keyed by owner_key ("user:<id>" or "guest:<random>"), not a plain
        # user_id FK, so guests can connect Google Photos too - see webapp.photo_owner_key.
        _c.execute("""CREATE TABLE IF NOT EXISTS photo_accounts(
            owner_key TEXT PRIMARY KEY,
            refresh_token TEXT, access_token TEXT, token_expiry TEXT, granted TEXT)""")
        # see the matching table in _init_schema_sqlite for what this is.
        _c.execute("""CREATE TABLE IF NOT EXISTS generations(
            id SERIAL PRIMARY KEY, identity TEXT NOT NULL, ts TIMESTAMPTZ NOT NULL)""")
        _pg_text_to_timestamptz(_c, "generations", "ts")
        _c.execute(
            "CREATE INDEX IF NOT EXISTS idx_generations_identity_ts ON generations(identity, ts)"
        )
        _c.execute("ALTER TABLE generations ADD COLUMN IF NOT EXISTS ip TEXT")
        _c.execute("CREATE INDEX IF NOT EXISTS idx_generations_ip_ts ON generations(ip, ts)")
