"""Unit tests for the forgot-password / reset-password flow (webapp.py).

mailer.send is monkeypatched instead of really calling Resend - this captures
the exact link MemoTrip would email a user, without a network call or a real
inbox, the same way mailer.py itself falls back to logging when
RESEND_API_KEY is unset (see mailer.py).
"""
from __future__ import annotations

import re
import urllib.parse


def _capture_reset_link(monkeypatch) -> dict:
    import mailer
    sent: dict = {}

    def fake_send(to, subject, html):
        sent["to"] = to
        sent["html"] = html

    monkeypatch.setattr(mailer, "send", fake_send)
    return sent


def _extract_token(html: str) -> str:
    m = re.search(r"reset-password\?token=([^\"&]+)", html)
    assert m, f"no reset link found in email body: {html}"
    return urllib.parse.unquote(m.group(1))


def test_forgot_password_unknown_email_gives_generic_response(client):
    r = client.post("/forgot-password", data={"email": "nobody@example.com"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/forgot-password?sent=1"


def test_full_password_reset_flow(client, signup, monkeypatch):
    email, old_password = signup()
    sent = _capture_reset_link(monkeypatch)

    r = client.post("/forgot-password", data={"email": email}, follow_redirects=False)
    assert r.status_code == 303
    assert sent["to"] == email
    token = _extract_token(sent["html"])

    new_password = "newpassword123"
    r = client.post("/reset-password", data={"token": token, "password": new_password}, follow_redirects=False)
    assert r.headers["location"] == "/"

    client.post("/logout")

    r = client.post("/login", data={"email": email, "password": old_password}, follow_redirects=False)
    assert "error=" in r.headers["location"]

    r = client.post("/login", data={"email": email, "password": new_password}, follow_redirects=False)
    assert r.headers["location"] == "/"


def test_reset_token_cannot_be_reused(client, signup, monkeypatch):
    email, _ = signup()
    sent = _capture_reset_link(monkeypatch)
    client.post("/forgot-password", data={"email": email})
    token = _extract_token(sent["html"])

    r = client.post("/reset-password", data={"token": token, "password": "firstnewpass"}, follow_redirects=False)
    assert r.headers["location"] == "/"

    r = client.post("/reset-password", data={"token": token, "password": "secondnewpass"}, follow_redirects=False)
    assert r.headers["location"] == "/forgot-password"


def test_reset_password_rejects_bad_length(client, signup, monkeypatch):
    email, _ = signup()
    sent = _capture_reset_link(monkeypatch)
    client.post("/forgot-password", data={"email": email})
    token = _extract_token(sent["html"])

    r = client.post("/reset-password", data={"token": token, "password": "abc"}, follow_redirects=False)
    assert "error=" in r.headers["location"]


def test_invalid_token_shows_error_page(client):
    r = client.get("/reset-password", params={"token": "not-a-real-token"})
    assert r.status_code == 200
    assert "לא תקין" in r.text
