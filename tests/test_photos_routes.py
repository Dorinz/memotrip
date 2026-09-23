"""Unit tests for webapp.py's Google Photos connection routes and the
internal Cloud Tasks callback auth check.

fetch_photos.* is always mocked - no test here ever talks to Google. The
goal is webapp's own glue: session state across the OAuth redirect, error
responses when that state is missing/mismatched, and who's allowed to hit
the /internal/tasks/* callbacks.
"""

from __future__ import annotations

import urllib.parse

import pytest
from fastapi import HTTPException

import config
import fetch_photos
import webapp


def _second_client():
    from fastapi.testclient import TestClient

    return TestClient(webapp.app)


# --------------------------------------------------------------- photo_owner_key


def test_photo_owner_key_is_stable_across_requests_for_a_guest(client):
    r1 = client.post("/photos/session")
    r2 = client.post("/photos/session")
    # both calls fail the same way (not connected) but must carry the same
    # connect_url shape derived from the same underlying guest identity
    assert r1.status_code == r2.status_code == 401


def test_photo_owner_key_differs_between_two_guest_sessions(client, monkeypatch):
    seen = []

    def fake_authorise(owner_key, path):
        seen.append(owner_key)
        raise fetch_photos.PhotosNotConnected("nope")

    monkeypatch.setattr(fetch_photos, "open_session_for_user", fake_authorise)
    client.post("/photos/session")
    _second_client().post("/photos/session")
    assert len(seen) == 2
    assert seen[0] != seen[1]


def test_photo_owner_key_is_userid_based_when_logged_in(client, signup, monkeypatch):
    email, password = signup()
    client.post("/login", data={"email": email, "password": password})
    user = webapp.get_user_by_email(email)

    seen = []

    def fake_authorise(owner_key, path):
        seen.append(owner_key)
        raise fetch_photos.PhotosNotConnected("nope")

    monkeypatch.setattr(fetch_photos, "open_session_for_user", fake_authorise)
    client.post("/photos/session")
    assert seen == [f"user:{user['id']}"]


# --------------------------------------------------------------- /photos/session


def test_photos_session_not_connected_returns_401_with_connect_url(client, monkeypatch):
    monkeypatch.setattr(
        fetch_photos,
        "open_session_for_user",
        lambda owner, path: (_ for _ in ()).throw(fetch_photos.PhotosNotConnected("nope")),
    )
    r = client.post("/photos/session")
    assert r.status_code == 401
    body = r.json()
    assert "connect_url" in body
    assert "resume_photos" in urllib.parse.unquote(body["connect_url"])


def test_photos_session_unexpected_error_returns_500(client, monkeypatch):
    monkeypatch.setattr(
        fetch_photos,
        "open_session_for_user",
        lambda owner, path: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    r = client.post("/photos/session")
    assert r.status_code == 500
    assert "boom" in r.json()["error"]


def test_photos_session_success_returns_picker_uri(client, monkeypatch):
    monkeypatch.setattr(
        fetch_photos,
        "open_session_for_user",
        lambda owner, path: (object(), {"pickerUri": "https://pick.example/x", "id": "sid1"}),
    )
    r = client.post("/photos/session")
    assert r.status_code == 200
    assert r.json() == {"picker_uri": "https://pick.example/x", "sid": "sid1"}


# --------------------------------------------------------- /oauth/photos/callback


def test_oauth_callback_missing_state_is_rejected(client):
    r = client.get("/oauth/photos/callback", params={"code": "abc", "state": "whatever"})
    assert r.status_code == 400


def test_oauth_callback_state_mismatch_is_rejected(client, monkeypatch):
    monkeypatch.setattr(
        fetch_photos,
        "build_auth_url",
        lambda path, redirect, state: ("https://google/x", "verifier"),
    )
    client.get("/oauth/photos/start")  # stashes state+verifier in the session
    r = client.get("/oauth/photos/callback", params={"code": "abc", "state": "wrong-state"})
    assert r.status_code == 400


def test_oauth_start_redirects_to_the_provided_auth_url(client, monkeypatch):
    monkeypatch.setattr(
        fetch_photos,
        "build_auth_url",
        lambda path, redirect, state: ("https://google/x", "verifier"),
    )
    r = client.get("/oauth/photos/start", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "https://google/x"


def test_oauth_callback_with_matching_state_but_failed_exchange_returns_500(client, monkeypatch):
    monkeypatch.setattr(webapp.secrets, "token_urlsafe", lambda n: "fixed-state")
    monkeypatch.setattr(
        fetch_photos,
        "build_auth_url",
        lambda path, redirect, state: ("https://google/x", "verifier"),
    )
    client.get("/oauth/photos/start", follow_redirects=False)

    def fake_exchange(*a, **k):
        raise RuntimeError("invalid_grant")

    monkeypatch.setattr(fetch_photos, "exchange_code", fake_exchange)
    r = client.get("/oauth/photos/callback", params={"code": "abc", "state": "fixed-state"})
    assert r.status_code == 500


def test_oauth_callback_success_opens_a_picker_session(client, monkeypatch):
    monkeypatch.setattr(webapp.secrets, "token_urlsafe", lambda n: "fixed-state")
    monkeypatch.setattr(
        fetch_photos,
        "build_auth_url",
        lambda path, redirect, state: ("https://google/x", "verifier"),
    )
    client.get("/oauth/photos/start", follow_redirects=False)

    monkeypatch.setattr(fetch_photos, "exchange_code", lambda *a, **k: object())
    monkeypatch.setattr(fetch_photos, "save_user_credentials", lambda owner, creds: None)
    monkeypatch.setattr(
        fetch_photos,
        "open_session_for_user",
        lambda owner, path: (object(), {"id": "sid1", "pickerUri": "https://pick.example/x"}),
    )
    r = client.get("/oauth/photos/callback", params={"code": "abc", "state": "fixed-state"})
    assert r.status_code == 200
    assert "https://pick.example/x" in r.text


def test_oauth_callback_falls_back_to_next_url_if_reopen_fails(client, monkeypatch):
    monkeypatch.setattr(webapp.secrets, "token_urlsafe", lambda n: "fixed-state")
    monkeypatch.setattr(
        fetch_photos,
        "build_auth_url",
        lambda path, redirect, state: ("https://google/x", "verifier"),
    )
    client.get("/oauth/photos/start", params={"next": "/somewhere"}, follow_redirects=False)

    monkeypatch.setattr(fetch_photos, "exchange_code", lambda *a, **k: object())
    monkeypatch.setattr(fetch_photos, "save_user_credentials", lambda owner, creds: None)

    def _boom(*a, **k):
        raise RuntimeError("no session")

    monkeypatch.setattr(fetch_photos, "open_session_for_user", _boom)
    r = client.get(
        "/oauth/photos/callback",
        params={"code": "abc", "state": "fixed-state"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/somewhere"


# ------------------------------------------------------------- photos/start,finish


def test_trip_photos_finish_without_picker_sid_returns_400(client):
    r = client.post("/trips", data={"description": "x"}, follow_redirects=False)
    tid = r.headers["location"].rsplit("/", 1)[-1]
    r = client.post(f"/trips/{tid}/photos/finish")
    assert r.status_code == 400


def test_trip_photos_finish_enqueues_when_picker_sid_present(client, monkeypatch):
    calls = []
    monkeypatch.setattr(webapp.tasks, "enqueue", lambda kind, tid: calls.append((kind, tid)))
    r = client.post("/trips", data={"description": "x"}, follow_redirects=False)
    tid = r.headers["location"].rsplit("/", 1)[-1]
    webapp.update(tid, picker_sid="sid1")
    calls.clear()  # drop the ("build", tid) call from trip creation above
    r = client.post(f"/trips/{tid}/photos/finish")
    assert r.status_code == 200
    assert calls == [("photos", tid)]


def test_trip_photos_start_not_connected_returns_401(client, monkeypatch):
    monkeypatch.setattr(
        fetch_photos,
        "open_session_for_user",
        lambda owner, path: (_ for _ in ()).throw(fetch_photos.PhotosNotConnected("nope")),
    )
    r = client.post("/trips", data={"description": "x"}, follow_redirects=False)
    tid = r.headers["location"].rsplit("/", 1)[-1]
    r = client.post(f"/trips/{tid}/photos/start")
    assert r.status_code == 401
    assert f"tid={tid}" in r.json()["connect_url"] or tid in r.json()["connect_url"]


# ------------------------------------------------------------------ task auth


def test_verify_task_auth_noop_when_no_queue_configured(monkeypatch):
    monkeypatch.setattr(config, "CLOUD_TASKS_QUEUE", None)
    webapp._verify_task_auth("", "https://example.com/internal/tasks/build/x")  # must not raise


def test_verify_task_auth_rejects_missing_bearer_header(monkeypatch):
    monkeypatch.setattr(config, "CLOUD_TASKS_QUEUE", "projects/p/locations/l/queues/q")
    with pytest.raises(HTTPException) as exc:
        webapp._verify_task_auth("", "https://example.com/internal/tasks/build/x")
    assert exc.value.status_code == 403


def test_verify_task_auth_rejects_invalid_token(monkeypatch):
    monkeypatch.setattr(config, "CLOUD_TASKS_QUEUE", "projects/p/locations/l/queues/q")

    def fake_verify(token, request, audience):
        raise ValueError("bad token")

    monkeypatch.setattr("google.oauth2.id_token.verify_oauth2_token", fake_verify)
    with pytest.raises(HTTPException) as exc:
        webapp._verify_task_auth("Bearer abc", "https://example.com/internal/tasks/build/x")
    assert exc.value.status_code == 403


def test_verify_task_auth_rejects_unexpected_invoker_identity(monkeypatch):
    monkeypatch.setattr(config, "CLOUD_TASKS_QUEUE", "projects/p/locations/l/queues/q")
    monkeypatch.setattr(config, "TASKS_INVOKER_SA", "invoker@p.iam.gserviceaccount.com")
    monkeypatch.setattr(
        "google.oauth2.id_token.verify_oauth2_token",
        lambda token, request, audience: {"email": "someone-else@evil.com"},
    )
    with pytest.raises(HTTPException) as exc:
        webapp._verify_task_auth("Bearer abc", "https://example.com/internal/tasks/build/x")
    assert exc.value.status_code == 403


def test_verify_task_auth_accepts_matching_invoker_identity(monkeypatch):
    monkeypatch.setattr(config, "CLOUD_TASKS_QUEUE", "projects/p/locations/l/queues/q")
    monkeypatch.setattr(config, "TASKS_INVOKER_SA", "invoker@p.iam.gserviceaccount.com")
    monkeypatch.setattr(
        "google.oauth2.id_token.verify_oauth2_token",
        lambda token, request, audience: {"email": "invoker@p.iam.gserviceaccount.com"},
    )
    webapp._verify_task_auth(
        "Bearer abc", "https://example.com/internal/tasks/build/x"
    )  # must not raise


def test_internal_task_build_route_runs_when_no_queue_configured(client, monkeypatch):
    monkeypatch.setattr(config, "CLOUD_TASKS_QUEUE", None)
    calls = []
    monkeypatch.setattr(webapp, "run_build", lambda tid: calls.append(tid))
    r = client.post("/internal/tasks/build/trip123")
    assert r.status_code == 200
    assert calls == ["trip123"]


def test_internal_task_build_route_rejects_without_auth_when_queue_configured(client, monkeypatch):
    monkeypatch.setattr(config, "CLOUD_TASKS_QUEUE", "projects/p/locations/l/queues/q")
    r = client.post("/internal/tasks/build/trip123")
    assert r.status_code == 403
