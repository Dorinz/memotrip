"""Shared pytest fixtures for MemoTrip's FastAPI app.

Each test gets its own throwaway sqlite file (db.DB monkeypatched per test),
so tests never see each other's data and never touch the real trips.db used
by local dev. Nothing here talks to Postgres, Cloud Tasks, Resend, or Google
Photos - those are only reached when their respective env vars are set,
which they never are in a test run.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


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
def client(tmp_path, monkeypatch, _stub_tasks_enqueue):
    import db

    monkeypatch.setattr(db, "DB", tmp_path / "test.db")
    db.init_schema()

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
