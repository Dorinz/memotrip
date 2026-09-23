"""Unit tests for mailer.py's Resend wrapper.

requests.post is always mocked - these tests never hit the network. The
behavior that matters most: no RESEND_API_KEY must never attempt a network
call (that's what keeps local dev working without a Resend account).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import config
import mailer


@pytest.fixture(autouse=True)
def _clean_key(monkeypatch):
    monkeypatch.setattr(config, "RESEND_API_KEY", None)


def test_send_without_api_key_does_not_call_requests(monkeypatch, capsys):
    def _boom(*a, **k):
        raise AssertionError("requests.post should not be called without RESEND_API_KEY")

    monkeypatch.setattr("requests.post", _boom)
    mailer.send("user@example.com", "subject", "<p>body</p>")
    out = capsys.readouterr().out
    assert "not sending" in out
    assert "user@example.com" in out


def test_send_with_api_key_posts_expected_payload(monkeypatch):
    monkeypatch.setattr(config, "RESEND_API_KEY", "re_test")
    captured = {}

    def fake_post(url, headers, json, timeout):
        captured.update(url=url, headers=headers, json=json, timeout=timeout)
        return SimpleNamespace(ok=True, raise_for_status=lambda: None)

    monkeypatch.setattr("requests.post", fake_post)
    mailer.send("user@example.com", "Subject", "<p>Hi</p>")

    assert captured["url"] == "https://api.resend.com/emails"
    assert captured["headers"]["Authorization"] == "Bearer re_test"
    assert captured["json"]["to"] == ["user@example.com"]
    assert captured["json"]["subject"] == "Subject"
    assert captured["json"]["html"] == "<p>Hi</p>"
    assert captured["json"]["from"] == mailer.FROM_ADDRESS


def test_send_raises_and_logs_body_on_non_ok_response(monkeypatch, capsys):
    monkeypatch.setattr(config, "RESEND_API_KEY", "re_test")

    def raise_for_status():
        raise RuntimeError("422 Client Error")

    resp = SimpleNamespace(
        ok=False,
        status_code=422,
        text='{"message":"domain not verified"}',
        raise_for_status=raise_for_status,
    )
    monkeypatch.setattr("requests.post", lambda *a, **k: resp)

    with pytest.raises(RuntimeError, match="422"):
        mailer.send("user@example.com", "subject", "body")

    out = capsys.readouterr().out
    assert "domain not verified" in out
