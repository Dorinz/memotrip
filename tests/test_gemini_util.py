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


# --------------------------------------------- unusable answers (stuck-model loops)


class _ScriptedModels:
    """Stands in for genai.Client().models: answers each call with the next
    (text, finish_reason) from `script`, and records each call's config."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0
        self.configs = []

    def generate_content(self, **kwargs):
        self.calls += 1
        self.configs.append(kwargs.get("config"))
        text, finish = self.script.pop(0)
        return SimpleNamespace(
            text=text,
            candidates=[SimpleNamespace(finish_reason=SimpleNamespace(name=finish))],
            usage_metadata=SimpleNamespace(candidates_token_count=100, thoughts_token_count=50),
        )


GOOD = (json.dumps({"timeline": [1, 2, 3]}), "STOP")


def _gen(models, **kw):
    return gemini_util.generate_json("model", "prompt", cl=_fake_client(models), **kw)


def test_answer_cut_off_at_the_token_limit_is_asked_for_again():
    models = _ScriptedModels([('{"timeline": [1, 2', "MAX_TOKENS"), GOOD])
    assert _gen(models, bad_retries=2, log=lambda m: None) == {"timeline": [1, 2, 3]}
    assert models.calls == 2


def test_broken_json_is_asked_for_again():
    models = _ScriptedModels([('{"timeline": ["key: lisbon1. key: lisb', "STOP"), GOOD])
    assert _gen(models, bad_retries=2, log=lambda m: None) == {"timeline": [1, 2, 3]}
    assert models.calls == 2


def test_answer_rejected_by_check_is_asked_for_again():
    models = _ScriptedModels([(json.dumps({"timeline": [1]}), "STOP"), GOOD])

    def check(d):
        return "too short" if len(d["timeline"]) < 3 else None

    assert _gen(models, check=check, bad_retries=2, log=lambda m: None) == {"timeline": [1, 2, 3]}
    assert models.calls == 2


def test_gives_up_with_bad_response_after_the_last_retry():
    models = _ScriptedModels([("{", "MAX_TOKENS")] * 3)
    with pytest.raises(gemini_util.BadResponse, match="token limit"):
        _gen(models, bad_retries=2, log=lambda m: None)
    assert models.calls == 3  # 1 try + 2 retries


def test_without_bad_retries_a_broken_answer_fails_at_once():
    models = _ScriptedModels([("not json", "STOP")])
    with pytest.raises(gemini_util.BadResponse, match="not valid JSON"):
        _gen(models, log=lambda m: None)
    assert models.calls == 1


def test_every_answer_logs_its_finish_reason_and_token_usage():
    models = _ScriptedModels([("{", "MAX_TOKENS"), GOOD])
    messages = []
    _gen(models, bad_retries=1, log=messages.append)
    assert "MAX_TOKENS · 100 out + 50 thinking tokens" in messages[0]
    assert "unusable: cut off at the output token limit" in messages[1]
    assert "STOP · 100 out + 50 thinking tokens" in messages[2]


def test_max_output_tokens_reaches_the_request_config():
    models = _ScriptedModels([GOOD])
    _gen(models, max_output_tokens=24576, log=lambda m: None)
    assert models.configs[0].max_output_tokens == 24576
