"""Unit tests for config.py's env-var reads.

config.py reads every value at import time, so each test reloads the module
under a patched os.environ instead of poking at already-imported constants.
"""

from __future__ import annotations

import importlib
import pathlib

import pytest


@pytest.fixture()
def reload_config(monkeypatch):
    """Reload config.py after the caller tweaks os.environ; reloads it back
    to a clean state afterwards so later tests never see a stale monkeypatch."""
    import config

    def _reload():
        importlib.reload(config)
        return config

    yield _reload
    for var in (
        "PUBLIC_BASE_URL",
        "DATABASE_URL",
        "CLOUD_TASKS_QUEUE",
        "TASKS_INVOKER_SA",
        "OAUTH_WEB_CLIENT_PATH",
        "RESEND_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    importlib.reload(config)


def test_defaults_when_env_unset(monkeypatch, reload_config):
    for var in (
        "PUBLIC_BASE_URL",
        "DATABASE_URL",
        "CLOUD_TASKS_QUEUE",
        "TASKS_INVOKER_SA",
        "RESEND_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    cfg = reload_config()

    assert cfg.PUBLIC_BASE_URL == "http://localhost:8000"
    assert cfg.DATABASE_URL is None
    assert cfg.CLOUD_TASKS_QUEUE is None
    assert cfg.TASKS_INVOKER_SA is None
    assert cfg.RESEND_API_KEY is None
    assert cfg.OAUTH_WEB_CLIENT_PATH.name == "credentials_web.json"


def test_env_values_are_picked_up(monkeypatch, reload_config):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://memotrip.app")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@host/db")
    monkeypatch.setenv("CLOUD_TASKS_QUEUE", "projects/p/locations/l/queues/q")
    monkeypatch.setenv("TASKS_INVOKER_SA", "invoker@p.iam.gserviceaccount.com")
    monkeypatch.setenv("RESEND_API_KEY", "re_test_key")
    cfg = reload_config()

    assert cfg.PUBLIC_BASE_URL == "https://memotrip.app"
    assert cfg.DATABASE_URL == "postgresql://u:p@host/db"
    assert cfg.CLOUD_TASKS_QUEUE == "projects/p/locations/l/queues/q"
    assert cfg.TASKS_INVOKER_SA == "invoker@p.iam.gserviceaccount.com"
    assert cfg.RESEND_API_KEY == "re_test_key"


def test_oauth_web_client_path_override(monkeypatch, reload_config):
    monkeypatch.setenv("OAUTH_WEB_CLIENT_PATH", "/secrets/creds.json")
    cfg = reload_config()

    assert cfg.OAUTH_WEB_CLIENT_PATH == pathlib.Path("/secrets/creds.json")
