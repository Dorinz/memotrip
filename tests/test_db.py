"""Unit tests for db.py's sqlite schema creation and migration dance.

Only the sqlite path is exercised - psycopg2 isn't installed in this dev
environment (it's a prod-only, lazily-imported dependency; DATABASE_URL is
never set in tests, see conftest.py's module docstring), so _init_schema_pg()
has no way to be run here without a real Postgres instance.
"""

from __future__ import annotations

import sqlite3

import pytest

import db


@pytest.fixture()
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "test.db"
    monkeypatch.setattr(db, "DB", path)
    return path


def _columns(conn, table):
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_init_schema_creates_all_tables(db_path):
    db.init_schema()
    with db.db() as c:
        tables = {r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"trips", "users", "photo_accounts"} <= tables


def test_init_schema_is_idempotent(db_path):
    db.init_schema()
    db.init_schema()  # must not raise on a second run against the same file
    with db.db() as c:
        tables = {r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"trips", "users", "photo_accounts"} <= tables


def test_trips_table_has_user_id_and_photo_owner_key_columns(db_path):
    db.init_schema()
    with db.db() as c:
        cols = _columns(c, "trips")
    assert "user_id" in cols
    assert "photo_owner_key" in cols


def test_migration_adds_user_id_to_a_pre_existing_trips_table(db_path):
    # simulate an old sqlite file created before user_id/photo_owner_key existed
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE trips(id TEXT PRIMARY KEY, created TEXT, description TEXT, "
        "region_hint TEXT, status TEXT, stage TEXT, log TEXT, error TEXT, "
        "picker_uri TEXT, picker_sid TEXT)"
    )
    conn.commit()
    conn.close()

    db.init_schema()

    with db.db() as c:
        cols = _columns(c, "trips")
    assert "user_id" in cols
    assert "photo_owner_key" in cols


def test_migration_renames_username_column_to_email(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE users(id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, created TEXT)"
    )
    conn.execute(
        "INSERT INTO users(username, password_hash, created) VALUES('a@b.com', 'h', 'now')"
    )
    conn.commit()
    conn.close()

    db.init_schema()

    with db.db() as c:
        cols = _columns(c, "users")
        row = c.execute("SELECT email FROM users").fetchone()
    assert "username" not in cols
    assert "email" in cols
    assert row["email"] == "a@b.com"


def test_migration_adds_display_name_to_a_pre_existing_users_table(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE users(id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "email TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, created TEXT)"
    )
    conn.commit()
    conn.close()

    db.init_schema()

    with db.db() as c:
        cols = _columns(c, "users")
    assert "display_name" in cols


def test_migration_drops_and_recreates_old_photo_accounts_table(db_path):
    conn = sqlite3.connect(db_path)
    # old shape: keyed by user_id, no owner_key column
    conn.execute("CREATE TABLE photo_accounts(user_id INTEGER PRIMARY KEY, refresh_token TEXT)")
    conn.execute("INSERT INTO photo_accounts(user_id, refresh_token) VALUES(1, 'stale-token')")
    conn.commit()
    conn.close()

    db.init_schema()

    with db.db() as c:
        cols = _columns(c, "photo_accounts")
        rows = c.execute("SELECT * FROM photo_accounts").fetchall()
    assert "owner_key" in cols
    assert "user_id" not in cols
    assert rows == []  # the old, unmigratable row is gone


def test_new_photo_accounts_table_is_not_touched_by_migration(db_path):
    db.init_schema()
    with db.db() as c:
        c.execute("INSERT INTO photo_accounts(owner_key, refresh_token) VALUES('user:1', 'tok')")
    db.init_schema()  # re-running must not drop a table that already has owner_key
    with db.db() as c:
        row = c.execute(
            "SELECT refresh_token FROM photo_accounts WHERE owner_key='user:1'"
        ).fetchone()
    assert row["refresh_token"] == "tok"


def test_db_connection_supports_dict_style_row_access(db_path):
    db.init_schema()
    with db.db() as c:
        c.execute(
            "INSERT INTO users(email, password_hash, created) VALUES(?,?,?)",
            ("a@b.com", "hash", "2026-01-01"),
        )
        row = c.execute("SELECT * FROM users WHERE email=?", ("a@b.com",)).fetchone()
    assert row["email"] == "a@b.com"


def test_db_context_manager_commits_on_success(db_path):
    db.init_schema()
    with db.db() as c:
        c.execute(
            "INSERT INTO users(email, password_hash, created) VALUES(?,?,?)",
            ("a@b.com", "hash", "2026-01-01"),
        )
    with db.db() as c:
        row = c.execute("SELECT * FROM users WHERE email=?", ("a@b.com",)).fetchone()
    assert row is not None


def test_generations_table_exists_with_identity_and_ts_columns(db_path):
    db.init_schema()
    with db.db() as c:
        tables = {r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        cols = _columns(c, "generations")
    assert "generations" in tables
    assert {"identity", "ts"} <= cols


def test_generations_table_survives_reinit_with_existing_rows(db_path):
    db.init_schema()
    with db.db() as c:
        c.execute(
            "INSERT INTO generations(identity, ts) VALUES(?,?)", ("guest:x", "2026-01-01 00:00:00")
        )
    db.init_schema()  # re-running must not drop/clear the table
    with db.db() as c:
        rows = c.execute("SELECT * FROM generations").fetchall()
    assert len(rows) == 1


def test_q_leaves_placeholders_untouched_on_sqlite_path():
    assert db._q("SELECT * FROM t WHERE id=?") == "SELECT * FROM t WHERE id=?"


def test_q_rewrites_placeholders_for_postgres(monkeypatch):
    monkeypatch.setattr(db, "_IS_PG", True)
    assert db._q("SELECT * FROM t WHERE id=?") == "SELECT * FROM t WHERE id=%s"


# ------------------------------------------------- foreign keys, trip_logs, UTC timestamps


def _user(c, email="a@b.com"):
    c.execute(
        "INSERT INTO users(email, password_hash, created) VALUES(?,?,?)",
        (email, "h", db.now_utc()),
    )
    return c.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()["id"]


def test_trip_user_id_must_point_at_a_real_user(db_path):
    db.init_schema()
    with pytest.raises(sqlite3.IntegrityError):
        with db.db() as c:
            c.execute("INSERT INTO trips(id, user_id) VALUES('t1', 999)")


def test_deleting_a_user_deletes_their_trips_and_logs(db_path):
    db.init_schema()
    with db.db() as c:
        uid = _user(c)
        c.execute("INSERT INTO trips(id, user_id) VALUES('t1', ?)", (uid,))
        c.execute("INSERT INTO trips(id) VALUES('guest-trip')")
        c.execute("INSERT INTO trip_logs(trip_id, ts, msg) VALUES('t1', ?, 'hi')", (db.now_utc(),))
    with db.db() as c:
        c.execute("DELETE FROM users WHERE id=?", (uid,))
    with db.db() as c:
        trips = [r["id"] for r in c.execute("SELECT id FROM trips")]
        logs = c.execute("SELECT COUNT(*) AS n FROM trip_logs").fetchone()["n"]
    assert trips == ["guest-trip"]
    assert logs == 0


def test_migration_rebuilds_old_trips_table_with_fk_and_moves_log_out(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE users(id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE NOT NULL, "
        "password_hash TEXT NOT NULL, display_name TEXT, created TEXT)"
    )
    conn.execute("INSERT INTO users(id, email, password_hash) VALUES(1, 'a@b.com', 'h')")
    conn.execute(
        "CREATE TABLE trips(id TEXT PRIMARY KEY, created TEXT, description TEXT, "
        "region_hint TEXT, status TEXT, stage TEXT, log TEXT, error TEXT, "
        "picker_uri TEXT, picker_sid TEXT, user_id INTEGER, photo_owner_key TEXT)"
    )
    conn.execute(
        "INSERT INTO trips(id, created, description, log, user_id) "
        "VALUES('owned', '2026-01-01 10:00', 'x', 'line 1\nline 2\n', 1)"
    )
    conn.execute("INSERT INTO trips(id, log, user_id) VALUES('orphan', '', 42)")
    conn.commit()
    conn.close()

    db.init_schema()
    db.init_schema()  # rerunning must not duplicate the moved log

    with db.db() as c:
        cols = _columns(c, "trips")
        fks = {r["table"] for r in c.execute("PRAGMA foreign_key_list(trips)")}
        trips = {r["id"]: r for r in c.execute("SELECT * FROM trips")}
        logs = c.execute("SELECT trip_id, msg FROM trip_logs").fetchall()
    assert "log" not in cols
    assert "users" in fks
    assert trips["owned"]["user_id"] == 1
    assert trips["owned"]["description"] == "x"
    assert trips["orphan"]["user_id"] is None  # its user never existed
    # the old local-time "HH:MM" value is now UTC with seconds
    assert len(trips["owned"]["created"]) == 19
    assert [(r["trip_id"], r["msg"]) for r in logs] == [("owned", "line 1\nline 2")]


def test_datetime_params_are_stored_as_utc_text(db_path):
    import datetime as dt

    db.init_schema()
    il = dt.timezone(dt.timedelta(hours=3))
    with db.db() as c:
        c.execute(
            "INSERT INTO trips(id, created) VALUES('t1', ?)",
            (dt.datetime(2026, 6, 15, 12, 30, tzinfo=il),),
        )
        row = c.execute("SELECT created FROM trips").fetchone()
    assert row["created"] == "2026-06-15 09:30:00"
    assert db.as_utc(row["created"]) == dt.datetime(2026, 6, 15, 9, 30, tzinfo=dt.timezone.utc)
