"""Unit tests for the per-identity daily cap on Gemini-backed generation
actions (webapp.quota_exceeded / record_generation), and its wiring into the
three routes that actually burn API calls: creating a trip, rerunning one,
and finishing a photo pick. conftest.py's `client` fixture stubs
tasks.enqueue, so hitting the limit is observed via the HTTP response /
stored trip state, never a real build.
"""

from __future__ import annotations

import datetime as dt

import webapp


def _second_client():
    from fastapi.testclient import TestClient

    return TestClient(webapp.app)


# ------------------------------------------------------------- pure helpers


def test_quota_message_matches_the_agreed_copy():
    assert webapp.QUOTA_MESSAGE == "אוף :( כבר ניצלת את המכסה היומית שלך, נסה שוב מאוחר יותר."


def test_israel_day_bounds_are_24_wall_clock_hours_apart():
    now = dt.datetime(2026, 6, 15, 10, 0, tzinfo=dt.timezone.utc)  # deep summer, no DST edge
    start, end = webapp._israel_day_bounds_utc(now)
    start_il = dt.datetime.strptime(start, "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=dt.timezone.utc
    ).astimezone(webapp.ISRAEL_TZ)
    end_il = dt.datetime.strptime(end, "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=dt.timezone.utc
    ).astimezone(webapp.ISRAEL_TZ)
    assert (start_il.hour, start_il.minute, start_il.second) == (0, 0, 0)
    assert end_il - start_il == dt.timedelta(days=1)


def test_israel_day_bounds_place_a_late_utc_evening_in_the_next_israel_day():
    # 22:30 UTC in June is already past midnight in Israel (UTC+3 in summer)
    now = dt.datetime(2026, 6, 15, 22, 30, tzinfo=dt.timezone.utc)
    start, _ = webapp._israel_day_bounds_utc(now)
    start_il = dt.datetime.strptime(start, "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=dt.timezone.utc
    ).astimezone(webapp.ISRAEL_TZ)
    assert start_il.date() == dt.date(2026, 6, 16)


def test_quota_exceeded_false_with_no_recorded_generations(client):
    assert webapp.quota_exceeded("guest:fresh") is False


def test_quota_exceeded_true_once_daily_limit_is_reached(client):
    identity = "guest:limit-test"
    for _ in range(webapp.DAILY_GENERATION_LIMIT - 1):
        webapp.record_generation(identity)
        assert webapp.quota_exceeded(identity) is False
    webapp.record_generation(identity)
    assert webapp.quota_exceeded(identity) is True


def test_quota_is_isolated_per_identity(client):
    for _ in range(webapp.DAILY_GENERATION_LIMIT):
        webapp.record_generation("guest:a")
    assert webapp.quota_exceeded("guest:a") is True
    assert webapp.quota_exceeded("guest:b") is False


def test_generations_from_a_different_israel_day_dont_count(client):
    identity = "guest:yesterday"
    old_ts = "2020-01-01 00:00:00"  # nowhere near "today" in any timezone
    with webapp.db() as c:
        for _ in range(webapp.DAILY_GENERATION_LIMIT + 1):
            c.execute("INSERT INTO generations(identity, ts) VALUES(?,?)", (identity, old_ts))
    assert webapp.quota_exceeded(identity) is False


# --------------------------------------------------------- /trips (create)


def test_fourth_trip_creation_in_a_day_is_blocked(client):
    for _ in range(webapp.DAILY_GENERATION_LIMIT):
        r = client.post("/trips", data={"description": "x"}, follow_redirects=False)
        assert r.status_code == 303
    r = client.post("/trips", data={"description": "x"}, follow_redirects=False)
    assert r.status_code == 429
    assert r.json()["error"] == webapp.QUOTA_MESSAGE


def test_quota_is_not_shared_between_two_guest_sessions(client):
    for _ in range(webapp.DAILY_GENERATION_LIMIT):
        client.post("/trips", data={"description": "x"}, follow_redirects=False)
    r = client.post("/trips", data={"description": "x"}, follow_redirects=False)
    assert r.status_code == 429

    other_guest = _second_client()
    r = other_guest.post("/trips", data={"description": "x"}, follow_redirects=False)
    assert r.status_code == 303  # a different guest has their own, untouched quota


def test_quota_applies_to_a_logged_in_user_too(client, signup):
    email, password = signup()
    client.post("/login", data={"email": email, "password": password})
    for _ in range(webapp.DAILY_GENERATION_LIMIT):
        r = client.post("/trips", data={"description": "x"}, follow_redirects=False)
        assert r.status_code == 303
    r = client.post("/trips", data={"description": "x"}, follow_redirects=False)
    assert r.status_code == 429


# -------------------------------------------------------------------- rerun


def test_rerun_counts_against_the_same_quota_as_create(client, monkeypatch):
    calls = []
    monkeypatch.setattr(webapp.tasks, "enqueue", lambda kind, tid: calls.append((kind, tid)))

    r = client.post("/trips", data={"description": "x"}, follow_redirects=False)  # 1/3
    tid = r.headers["location"].rsplit("/", 1)[-1]
    client.post(f"/trips/{tid}/rerun", follow_redirects=False)  # 2/3
    client.post(f"/trips/{tid}/rerun", follow_redirects=False)  # 3/3
    assert len(calls) == 3

    r = client.post(f"/trips/{tid}/rerun", follow_redirects=False)  # blocked
    assert r.status_code == 303  # a <form> post - the block shows via trip.error, not a JSON 4xx
    assert len(calls) == 3  # no new build was enqueued
    assert webapp.get_trip(tid)["error"] == webapp.QUOTA_MESSAGE


def test_rerun_on_a_nonexistent_trip_with_quota_left_still_enqueues(client, monkeypatch):
    calls = []
    monkeypatch.setattr(webapp.tasks, "enqueue", lambda kind, tid: calls.append((kind, tid)))
    r = client.post("/trips/doesnotexist/rerun", follow_redirects=False)
    assert r.status_code == 303
    assert calls == [("build", "doesnotexist")]


# ------------------------------------------------------------- photos/finish


def test_photos_finish_counts_against_the_shared_quota(client, monkeypatch):
    calls = []
    monkeypatch.setattr(webapp.tasks, "enqueue", lambda kind, tid: calls.append((kind, tid)))

    r = client.post("/trips", data={"description": "x"}, follow_redirects=False)  # 1/3
    tid = r.headers["location"].rsplit("/", 1)[-1]
    webapp.update(tid, picker_sid="sid1")
    calls.clear()  # drop the ("build", tid) call from creation above

    r = client.post(f"/trips/{tid}/photos/finish")  # 2/3
    assert r.status_code == 200
    r = client.post(f"/trips/{tid}/photos/finish")  # 3/3
    assert r.status_code == 200
    assert calls == [("photos", tid), ("photos", tid)]

    r = client.post(f"/trips/{tid}/photos/finish")  # blocked
    assert r.status_code == 429
    assert r.json()["error"] == webapp.QUOTA_MESSAGE
    assert calls == [("photos", tid), ("photos", tid)]  # no third call went through


def test_photos_finish_without_picker_sid_is_rejected_before_touching_quota(client):
    r = client.post("/trips", data={"description": "x"}, follow_redirects=False)  # 1/3
    tid = r.headers["location"].rsplit("/", 1)[-1]

    r = client.post(f"/trips/{tid}/photos/finish")
    assert r.status_code == 400  # no picker_sid set - rejected first, quota untouched

    # confirm quota is still intact: 2 more creates should succeed (total 3/3)
    for _ in range(webapp.DAILY_GENERATION_LIMIT - 1):
        r = client.post("/trips", data={"description": "x"}, follow_redirects=False)
        assert r.status_code == 303
    r = client.post("/trips", data={"description": "x"}, follow_redirects=False)
    assert r.status_code == 429
