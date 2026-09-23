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


def test_q_leaves_placeholders_untouched_on_sqlite_path():
    assert db._q("SELECT * FROM t WHERE id=?") == "SELECT * FROM t WHERE id=?"


def test_q_rewrites_placeholders_for_postgres(monkeypatch):
    monkeypatch.setattr(db, "_IS_PG", True)
    assert db._q("SELECT * FROM t WHERE id=?") == "SELECT * FROM t WHERE id=%s"
