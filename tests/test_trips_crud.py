"""Unit tests for webapp.py's trip creation, ownership and deletion routes.

conftest.py's `client` fixture stubs tasks.enqueue by default, so no test
here ever spins up a real background thread or touches the Gemini/Google
Photos pipeline - these tests are only about the DB rows, ownership checks
and filesystem side effects webapp.py itself is responsible for.
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

import webapp


def _second_client():
    """A brand-new TestClient against the SAME (already monkeypatched) DB -
    simulates a second, independent browser session/guest."""
    return TestClient(webapp.app)


# --------------------------------------------------------------------- create


def test_create_trip_requires_description_or_a_document(client):
    r = client.post("/trips", data={"description": ""})
    assert r.status_code == 400


def test_create_trip_with_description_only_succeeds(client):
    r = client.post("/trips", data={"description": "טיול לאיים האזוריים"}, follow_redirects=False)
    assert r.status_code == 303
    tid = r.headers["location"].rsplit("/", 1)[-1]
    assert webapp.get_trip(tid) is not None


def test_create_trip_with_only_a_document_succeeds(client):
    files = [("docs", ("itinerary.docx", b"fake docx bytes", "application/octet-stream"))]
    r = client.post("/trips", data={"description": ""}, files=files, follow_redirects=False)
    assert r.status_code == 303


def test_create_trip_sanitizes_a_path_traversal_filename(client, tmp_path):
    files = [("docs", ("../../evil.txt", b"payload", "text/plain"))]
    r = client.post("/trips", data={"description": "x"}, files=files, follow_redirects=False)
    tid = r.headers["location"].rsplit("/", 1)[-1]
    docs_dir = webapp.trip_dir(tid) / "docs"
    saved = list(docs_dir.iterdir())
    assert len(saved) == 1
    assert saved[0].name == "evil.txt"  # traversal stripped, stayed inside docs/
    assert saved[0].parent == docs_dir


def test_create_trip_as_guest_leaves_user_id_null(client):
    r = client.post("/trips", data={"description": "x"}, follow_redirects=False)
    tid = r.headers["location"].rsplit("/", 1)[-1]
    assert webapp.get_trip(tid)["user_id"] is None


def test_create_trip_as_logged_in_user_sets_user_id(client, signup):
    email, password = signup()
    client.post("/login", data={"email": email, "password": password})
    r = client.post("/trips", data={"description": "x"}, follow_redirects=False)
    tid = r.headers["location"].rsplit("/", 1)[-1]
    user = webapp.get_user_by_email(email)
    assert webapp.get_trip(tid)["user_id"] == user["id"]


# ---------------------------------------------------------------------- fetch


def test_get_nonexistent_trip_is_404(client):
    r = client.get("/trips/doesnotexist")
    assert r.status_code == 404


def test_status_of_nonexistent_trip_is_404(client):
    r = client.get("/trips/doesnotexist/status")
    assert r.status_code == 404


def test_status_reports_stored_fields(client):
    r = client.post("/trips", data={"description": "x"}, follow_redirects=False)
    tid = r.headers["location"].rsplit("/", 1)[-1]
    r = client.get(f"/trips/{tid}/status")
    body = r.json()
    assert body["status"] == "queued"


# ------------------------------------------------------------------------ rerun


def test_rerun_nonexistent_trip_still_enqueues_without_crashing(client, monkeypatch):
    calls = []
    monkeypatch.setattr(webapp.tasks, "enqueue", lambda kind, tid: calls.append((kind, tid)))
    r = client.post("/trips/doesnotexist/rerun", follow_redirects=False)
    assert r.status_code == 303
    assert calls == [("build", "doesnotexist")]


# -------------------------------------------------------------- theme reroll


def test_reroll_theme_without_a_built_spec_returns_400(client):
    r = client.post("/trips", data={"description": "x"}, follow_redirects=False)
    tid = r.headers["location"].rsplit("/", 1)[-1]
    r = client.post(f"/trips/{tid}/theme/reroll")
    assert r.status_code == 400


def test_reroll_theme_changes_the_stored_theme(client):
    r = client.post("/trips", data={"description": "x"}, follow_redirects=False)
    tid = r.headers["location"].rsplit("/", 1)[-1]
    spec_path = webapp.trip_dir(tid) / "spec.json"
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(
        json.dumps({"timeline": [], "theme": {"ink": "#000000"}}), encoding="utf-8"
    )

    r = client.post(f"/trips/{tid}/theme/reroll")
    assert r.status_code == 200
    new_spec = json.loads(spec_path.read_text(encoding="utf-8"))
    assert new_spec["theme"]["ink"] != "#000000"


# ----------------------------------------------------------------------- delete


def test_delete_nonexistent_trip_is_404(client):
    r = client.post("/trips/doesnotexist/delete")
    assert r.status_code == 404


def test_guest_can_delete_their_own_trip(client):
    r = client.post("/trips", data={"description": "x"}, follow_redirects=False)
    tid = r.headers["location"].rsplit("/", 1)[-1]
    r = client.post(f"/trips/{tid}/delete")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert webapp.get_trip(tid) is None
    assert not webapp.trip_dir(tid).exists()


def test_a_different_guest_session_cannot_delete_someone_elses_trip(client):
    r = client.post("/trips", data={"description": "x"}, follow_redirects=False)
    tid = r.headers["location"].rsplit("/", 1)[-1]

    stranger = _second_client()
    r = stranger.post(f"/trips/{tid}/delete")
    assert r.status_code == 403
    assert webapp.get_trip(tid) is not None  # untouched


def test_logged_in_user_cannot_delete_another_users_trip(client, signup):
    owner_email, owner_password = signup(email="owner@example.com")
    client.post("/login", data={"email": owner_email, "password": owner_password})
    r = client.post("/trips", data={"description": "x"}, follow_redirects=False)
    tid = r.headers["location"].rsplit("/", 1)[-1]
    client.post("/logout")

    other = _second_client()
    other.post("/signup", data={"email": "other@example.com", "password": "goodpass"})
    r = other.post(f"/trips/{tid}/delete")
    assert r.status_code == 403


def test_logged_in_user_cannot_delete_a_guest_trip(client, signup):
    guest = _second_client()
    r = guest.post("/trips", data={"description": "guest trip"}, follow_redirects=False)
    tid = r.headers["location"].rsplit("/", 1)[-1]

    email, password = signup()
    client.post("/login", data={"email": email, "password": password})
    r = client.post(f"/trips/{tid}/delete")
    assert r.status_code == 403


def test_delete_survives_a_missing_trip_folder(client):
    r = client.post("/trips", data={"description": "x"}, follow_redirects=False)
    tid = r.headers["location"].rsplit("/", 1)[-1]
    import shutil

    shutil.rmtree(webapp.trip_dir(tid))  # folder already gone somehow
    r = client.post(f"/trips/{tid}/delete")
    assert r.status_code == 200
    assert webapp.get_trip(tid) is None


def test_index_page_lists_only_the_logged_in_users_own_trips(client, signup):
    email, password = signup()
    client.post("/login", data={"email": email, "password": password})
    client.post("/trips", data={"description": "my own trip"}, follow_redirects=False)

    guest = _second_client()
    guest.post("/trips", data={"description": "a guest trip"}, follow_redirects=False)

    r = client.get("/")
    assert "my own trip" in r.text or "(ללא תיאור)" in r.text
    assert "a guest trip" not in r.text
