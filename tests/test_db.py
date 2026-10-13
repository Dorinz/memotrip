"""Unit tests for db.py against the test Postgres database (see conftest.py)."""

from __future__ import annotations

import datetime as dt

import psycopg2
import pytest

import db


def _tables(c):
    return {
        r["table_name"]
        for r in c.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()"
        ).fetchall()
    }


def _user(c, email="a@b.com"):
    return c.execute(
        "INSERT INTO users(email, password_hash, created) VALUES(?,?,?) RETURNING id",
        (email, "h", db.now_utc()),
    ).fetchone()["id"]


def test_init_schema_creates_all_tables():
    with db.db() as c:
        tables = _tables(c)
    assert {"users", "trips", "trip_logs", "photo_accounts", "generations"} <= tables


def test_init_schema_is_idempotent_and_keeps_data():
    with db.db() as c:
        _user(c)
    db.init_schema()  # must not raise, drop or clear anything on a rerun
    with db.db() as c:
        assert c.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"] == 1


def test_db_context_manager_commits_on_success():
    with db.db() as c:
        _user(c)
    with db.db() as c:
        row = c.execute("SELECT * FROM users WHERE email=?", ("a@b.com",)).fetchone()
    assert row["email"] == "a@b.com"  # dict-style row access


def test_db_context_manager_rolls_back_on_error():
    with pytest.raises(RuntimeError):
        with db.db() as c:
            _user(c)
            raise RuntimeError
    with db.db() as c:
        assert c.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"] == 0


def test_trip_user_id_must_point_at_a_real_user():
    with pytest.raises(psycopg2.errors.ForeignKeyViolation):
        with db.db() as c:
            c.execute("INSERT INTO trips(id, user_id) VALUES('t1', 999)")


def test_deleting_a_user_deletes_their_trips_and_logs():
    with db.db() as c:
        uid = _user(c)
        c.execute("INSERT INTO trips(id, user_id) VALUES('t1', ?)", (uid,))
        c.execute("INSERT INTO trips(id) VALUES('guest-trip')")
        c.execute("INSERT INTO trip_logs(trip_id, ts, msg) VALUES('t1', ?, 'hi')", (db.now_utc(),))
    with db.db() as c:
        c.execute("DELETE FROM users WHERE id=?", (uid,))
    with db.db() as c:
        trips = [r["id"] for r in c.execute("SELECT id FROM trips").fetchall()]
        logs = c.execute("SELECT COUNT(*) AS n FROM trip_logs").fetchone()["n"]
    assert trips == ["guest-trip"]
    assert logs == 0


def test_timestamps_round_trip_as_the_same_utc_moment():
    il = dt.timezone(dt.timedelta(hours=3))
    with db.db() as c:
        c.execute(
            "INSERT INTO trips(id, created) VALUES('t1', ?)",
            (dt.datetime(2026, 6, 15, 12, 30, tzinfo=il),),
        )
        row = c.execute("SELECT created FROM trips").fetchone()
    assert db.as_utc(row["created"]) == dt.datetime(2026, 6, 15, 9, 30, tzinfo=dt.timezone.utc)
    assert db.as_utc(None) is None


def test_q_rewrites_placeholders_for_psycopg2():
    assert db._q("SELECT * FROM t WHERE id=?") == "SELECT * FROM t WHERE id=%s"
