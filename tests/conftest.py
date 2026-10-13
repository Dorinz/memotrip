"""Shared pytest fixtures for MemoTrip's FastAPI app.

Tests run against their own Postgres database, memotrip_test - by default on
the local docker-compose server (`docker compose up -d`), or wherever
TEST_DATABASE_URL points (Cloud Build). It is created if missing, and every
table is emptied before each test, so tests never see each other's data and
never touch the dev database in .env. Nothing here talks to Cloud Tasks,
Resend, or Google Photos - those are only reached when their respective env
vars are set, which they never are in a test run.
"""

from __future__ import annotations

import os
import pathlib
import sys
import time
import urllib.parse

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://memotrip:memotrip@127.0.0.1:5432/memotrip_test"
)
# the tests empty every table - refuse anything that isn't a test database
assert urllib.parse.urlparse(TEST_DATABASE_URL).path.endswith("_test"), TEST_DATABASE_URL
# set before anything imports config (which reads it once) - and since
# local_env only fills in unset variables, .env's dev DATABASE_URL never wins
os.environ["DATABASE_URL"] = TEST_DATABASE_URL


def _create_test_database() -> None:
    import psycopg2

    url = urllib.parse.urlparse(TEST_DATABASE_URL)
    name = url.path.lstrip("/")
    # a just-started container (`docker compose up`, Cloud Build's postgres
    # step) takes a few seconds before it accepts connections
    for attempt in range(30):
        try:
            conn = psycopg2.connect(url._replace(path="/postgres").geturl())
            break
        except psycopg2.OperationalError:
            if attempt == 29:
                raise
            time.sleep(1)
    conn.autocommit = True  # CREATE DATABASE can't run inside a transaction
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM pg_database WHERE datname=%s", (name,))
        if not cur.fetchone():
            cur.execute(f'CREATE DATABASE "{name}"')
    finally:
        conn.close()


_create_test_database()

import db  # noqa: E402

db.init_schema()

_TABLES = "users, trips, trip_logs, photo_accounts, generations"


@pytest.fixture(autouse=True)
def _empty_tables():
    with db.db() as c:
        c.execute(f"TRUNCATE {_TABLES} RESTART IDENTITY CASCADE")


@pytest.fixture()
def _stub_tasks_enqueue(monkeypatch):
    """POSTing to /trips normally calls tasks.enqueue(), which - with no
    CLOUD_TASKS_QUEUE configured (always true in tests) - starts a real
    daemon thread running the full build pipeline, including a live call to
    Gemini using whatever GEMINI_API_KEY happens to be in this machine's
    .env. Stubbed out by default for every test using the `client` fixture
    below, so no test accidentally hits the network from a background thread
    that outlives (and can crash after) the test itself; a test of
    tasks.enqueue()/the pipeline routes on its own terms (see test_tasks.py,
    which never uses `client`) is unaffected, and a `client`-based test can
    still re-monkeypatch tasks.enqueue locally to observe the call."""
    import tasks

    monkeypatch.setattr(tasks, "enqueue", lambda kind, tid: None)


@pytest.fixture()
def client(_stub_tasks_enqueue):
    import webapp
    from fastapi.testclient import TestClient

    return TestClient(webapp.app)


@pytest.fixture()
def signup(client):
    """Create an account through the real /signup endpoint and return (email, password)."""

    def _signup(email: str = "test@example.com", password: str = "correcthorse"):
        r = client.post("/signup", data={"email": email, "password": password})
        assert r.status_code == 200, r.text
        return email, password

    return _signup
