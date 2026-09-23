"""Unit tests for webapp.py's signup/login/logout routes and password helpers.

Uses the `client` fixture from conftest.py (a throwaway sqlite DB per test),
so none of this touches the real trips.db used by local dev.
"""

from __future__ import annotations

import urllib.parse

import webapp

# --------------------------------------------------------------------- signup


def test_signup_rejects_invalid_email(client):
    r = client.post(
        "/signup", data={"email": "not-an-email", "password": "goodpass"}, follow_redirects=False
    )
    assert r.status_code == 303
    assert "error=" in r.headers["location"]
    assert webapp.get_user_by_email("not-an-email") is None


def test_signup_rejects_script_injection_in_email(client):
    r = client.post(
        "/signup",
        data={"email": "<script>@evil.com", "password": "goodpass"},
        follow_redirects=False,
    )
    assert "error=" in r.headers["location"]


def test_signup_rejects_too_short_password(client):
    r = client.post("/signup", data={"email": "a@b.com", "password": "abc"}, follow_redirects=False)
    assert "error=" in r.headers["location"]
    assert webapp.get_user_by_email("a@b.com") is None


def test_signup_rejects_password_over_bcrypt_limit(client):
    too_long = "x" * (webapp.MAX_PASSWORD_LEN + 1)
    r = client.post(
        "/signup", data={"email": "a@b.com", "password": too_long}, follow_redirects=False
    )
    assert "error=" in r.headers["location"]


def test_signup_accepts_password_at_max_length(client):
    exactly_max = "x" * webapp.MAX_PASSWORD_LEN
    r = client.post(
        "/signup", data={"email": "a@b.com", "password": exactly_max}, follow_redirects=False
    )
    assert r.headers["location"] == "/"


def test_signup_normalizes_email_case_and_whitespace(client):
    client.post("/signup", data={"email": "  Test@Example.COM ", "password": "goodpass"})
    assert webapp.get_user_by_email("test@example.com") is not None


def test_signup_duplicate_email_is_rejected(client, signup):
    email, _ = signup()
    client.post("/logout")  # signup() auto-logs in; log out so the retry hits the real check
    r = client.post(
        "/signup", data={"email": email, "password": "anotherpass"}, follow_redirects=False
    )
    assert "error=" in r.headers["location"]


def test_signup_duplicate_email_race_condition_is_handled(client, monkeypatch):
    """Simulates a concurrent signup for the same email that commits its row
    right after webapp's own pre-check ran (and saw nobody) but before this
    request's own INSERT executes: the DB's unique constraint is what
    actually catches the duplicate, and the loser must still get the same
    friendly "already exists" redirect instead of a raw 500."""
    email = "race@example.com"
    real_get_user_by_email = webapp.get_user_by_email
    calls = {"n": 0}

    def fake_get_user_by_email(e):
        calls["n"] += 1
        if calls["n"] == 1:
            return None  # this request's own pre-check: race not visible yet
        return real_get_user_by_email(e)  # the except-block's re-check, after the race lands

    monkeypatch.setattr(webapp, "get_user_by_email", fake_get_user_by_email)

    # the "concurrent" signup that won the race, landing its row first
    with webapp.db() as c:
        c.execute(
            "INSERT INTO users(email, password_hash, created) VALUES(?,?,?)",
            (email, webapp.hash_password("whatever"), "2026-01-01"),
        )

    r = client.post(
        "/signup", data={"email": email, "password": "goodpass"}, follow_redirects=False
    )
    location = urllib.parse.unquote(r.headers["location"])
    assert "error=" in location
    assert "כבר יש חשבון" in location


def test_signup_already_logged_in_redirects_home_without_creating_account(client, signup):
    email, password = signup()
    client.post("/login", data={"email": email, "password": password})
    r = client.post(
        "/signup",
        data={"email": "someoneelse@example.com", "password": "goodpass"},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/"
    assert webapp.get_user_by_email("someoneelse@example.com") is None


# ---------------------------------------------------------------------- login


def test_login_with_wrong_password_fails(client, signup):
    email, _ = signup()
    client.post("/logout")  # signup() auto-logs in; log out so this hits the real check
    r = client.post("/login", data={"email": email, "password": "wrong"}, follow_redirects=False)
    assert "error=" in r.headers["location"]


def test_login_with_unknown_email_fails_with_generic_message(client):
    r = client.post(
        "/login",
        data={"email": "nobody@example.com", "password": "whatever"},
        follow_redirects=False,
    )
    assert "error=" in r.headers["location"]


def test_login_is_case_insensitive_on_email(client, signup):
    email, password = signup(email="mixed@Example.com")
    client.post("/logout")  # signup() auto-logs in; log out so this hits the real check
    r = client.post(
        "/login",
        data={"email": "MIXED@example.COM", "password": password},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/"


def test_login_already_logged_in_redirects_home(client, signup):
    email, password = signup()
    client.post("/login", data={"email": email, "password": password})
    r = client.post(
        "/login",
        data={"email": email, "password": "wrong-but-irrelevant"},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/"


def test_logout_clears_session(client, signup):
    email, password = signup()
    client.post("/login", data={"email": email, "password": password})
    client.post("/logout")
    r = client.get("/login")
    assert r.status_code == 200  # not redirected away -> no longer authenticated


# ----------------------------------------------------------- password helpers


def test_verify_password_with_correct_password():
    hashed = webapp.hash_password("correct horse")
    assert webapp.verify_password("correct horse", hashed) is True


def test_verify_password_with_wrong_password():
    hashed = webapp.hash_password("correct horse")
    assert webapp.verify_password("wrong", hashed) is False


def test_verify_password_with_malformed_hash_returns_false_not_raise():
    assert webapp.verify_password("anything", "not-a-real-bcrypt-hash") is False


def test_email_regex_rejects_missing_tld():
    assert webapp.EMAIL_RE.match("a@b") is None


def test_email_regex_accepts_plus_addressing():
    assert webapp.EMAIL_RE.match("a+tag@example.com") is not None
