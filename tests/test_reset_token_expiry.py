"""Unit tests for webapp.py's password-reset token signing/verification,
focused on time-based expiry and payload tampering - the parts
test_password_reset.py's HTTP-level flow tests don't reach because they
never wait for the real clock to move.

itsdangerous.timed.TimestampSigner reads the wall clock via the module-level
`time.time()` call inside itsdangerous.timed itself, so monkeypatching that
module's own `time` reference (not the global time module) lets a token be
minted "in the past" without an actual sleep().
"""

from __future__ import annotations

import itsdangerous.timed

import webapp


class _FrozenTime:
    def __init__(self, real_time):
        self._t = real_time

    def time(self):
        return self._t


def test_verify_reset_token_accepts_a_fresh_token(client, signup):
    email, _ = signup()
    user = webapp.get_user_by_email(email)
    token = webapp._make_reset_token(user)
    assert webapp._verify_reset_token(token) is not None


def test_verify_reset_token_rejects_an_expired_token(client, signup, monkeypatch):
    email, _ = signup()
    user = webapp.get_user_by_email(email)

    real_time = itsdangerous.timed.time.time()
    monkeypatch.setattr(itsdangerous.timed, "time", _FrozenTime(real_time))
    token = webapp._make_reset_token(user)

    monkeypatch.setattr(
        itsdangerous.timed, "time", _FrozenTime(real_time + webapp.RESET_TOKEN_MAX_AGE + 1)
    )
    assert webapp._verify_reset_token(token) is None


def test_verify_reset_token_accepts_a_token_just_under_the_max_age(client, signup, monkeypatch):
    email, _ = signup()
    user = webapp.get_user_by_email(email)

    real_time = itsdangerous.timed.time.time()
    monkeypatch.setattr(itsdangerous.timed, "time", _FrozenTime(real_time))
    token = webapp._make_reset_token(user)

    monkeypatch.setattr(
        itsdangerous.timed, "time", _FrozenTime(real_time + webapp.RESET_TOKEN_MAX_AGE - 1)
    )
    assert webapp._verify_reset_token(token) is not None


def test_verify_reset_token_rejects_a_token_for_a_deleted_user(client, signup):
    email, _ = signup()
    user = webapp.get_user_by_email(email)
    token = webapp._make_reset_token(user)

    with webapp.db() as c:
        c.execute("DELETE FROM users WHERE id=?", (user["id"],))

    assert webapp._verify_reset_token(token) is None


def test_verify_reset_token_rejects_malformed_payload(client, signup):
    email, _ = signup()
    # a validly-signed token whose payload has no ":" separator at all
    bogus = webapp._reset_serializer.dumps("not-a-valid-payload")
    assert webapp._verify_reset_token(bogus) is None


def test_verify_reset_token_rejects_garbage_string():
    assert webapp._verify_reset_token("this-is-not-a-signed-token-at-all") is None


def test_verify_reset_token_rejects_empty_string():
    assert webapp._verify_reset_token("") is None
