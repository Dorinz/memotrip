"""Unit tests for gemini_util.py: key detection and the retry/backoff loop
around Gemini calls. The retry logic is the one bit of custom control flow
here (client construction and the actual API call are thin passthroughs to
google-genai), so it gets the most scrutiny.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import gemini_util


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)


def test_have_key_prefers_gemini_api_key(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "g-key")
    monkeypatch.setenv("GOOGLE_API_KEY", "google-key")
    assert gemini_util.have_key() == "g-key"


def test_have_key_falls_back_to_google_api_key(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "google-key")
    assert gemini_util.have_key() == "google-key"


def test_have_key_is_none_when_unset():
    assert gemini_util.have_key() is None


def test_client_raises_without_a_key():
    with pytest.raises(RuntimeError):
        gemini_util.client()


class _FakeModels:
    """Stands in for genai.Client().models: fails `fail_times` times with a
    given exception, then returns a canned JSON response."""

    def __init__(self, exc_factory, fail_times, payload):
        self.exc_factory = exc_factory
        self.fail_times = fail_times
        self.payload = payload
        self.calls = 0

    def generate_content(self, **kwargs):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.exc_factory()
        return SimpleNamespace(text=json.dumps(self.payload))


def _fake_client(models):
    return SimpleNamespace(models=models)


def test_generate_json_returns_parsed_payload_on_first_try(monkeypatch):
    models = _FakeModels(lambda: RuntimeError("unused"), fail_times=0, payload={"ok": True})
    result = gemini_util.generate_json(
        "model", "prompt", cl=_fake_client(models), log=lambda m: None
    )
    assert result == {"ok": True}
    assert models.calls == 1


def test_generate_json_retries_transient_errors(monkeypatch):
    models = _FakeModels(lambda: RuntimeError("503 UNAVAILABLE"), fail_times=2, payload={"n": 1})
    monkeypatch.setattr(gemini_util.time, "sleep", lambda s: None)
    result = gemini_util.generate_json(
        "model", "prompt", cl=_fake_client(models), retries=5, log=lambda m: None
    )
    assert result == {"n": 1}
    assert models.calls == 3


def test_generate_json_gives_up_after_max_retries(monkeypatch):
    models = _FakeModels(lambda: RuntimeError("429 rate limited"), fail_times=99, payload={})
    monkeypatch.setattr(gemini_util.time, "sleep", lambda s: None)
    with pytest.raises(RuntimeError, match="429"):
        gemini_util.generate_json(
            "model", "prompt", cl=_fake_client(models), retries=3, log=lambda m: None
        )
    assert models.calls == 3


def test_generate_json_does_not_retry_non_transient_errors(monkeypatch):
    models = _FakeModels(lambda: ValueError("totally unrelated failure"), fail_times=99, payload={})
    monkeypatch.setattr(gemini_util.time, "sleep", lambda s: None)
    with pytest.raises(ValueError, match="unrelated"):
        gemini_util.generate_json(
            "model", "prompt", cl=_fake_client(models), retries=5, log=lambda m: None
        )
    assert models.calls == 1  # failed fast, no retries burned on a non-transient error


def test_generate_json_logs_each_retry_attempt(monkeypatch):
    models = _FakeModels(lambda: RuntimeError("500 internal error"), fail_times=1, payload={})
    monkeypatch.setattr(gemini_util.time, "sleep", lambda s: None)
    messages = []
    gemini_util.generate_json(
        "model", "prompt", cl=_fake_client(models), retries=3, log=messages.append
    )
    assert len(messages) == 1
    assert "attempt 1/3" in messages[0]
