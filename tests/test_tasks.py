"""Unit tests for tasks.py's dispatch between the local threading.Thread
fallback and the Cloud Tasks queue.

The google-cloud-tasks package isn't installed in this dev environment (it's
only needed in prod, imported lazily) - so the Cloud Tasks path is exercised
against a fake module injected into sys.modules rather than the real SDK.
"""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import pytest

import config
import tasks


@pytest.fixture(autouse=True)
def _clean_queue(monkeypatch):
    monkeypatch.setattr(config, "CLOUD_TASKS_QUEUE", None)


def test_enqueue_without_queue_configured_starts_a_local_thread(monkeypatch):
    started = {}

    class FakeThread:
        def __init__(self, target, args, daemon):
            started["target"] = target
            started["args"] = args
            started["daemon"] = daemon

        def start(self):
            started["started"] = True

    monkeypatch.setattr(tasks.threading, "Thread", FakeThread)

    import webapp

    monkeypatch.setattr(webapp, "run_build", lambda tid: None)
    tasks.enqueue("build", "trip123")

    assert started["target"] is webapp.run_build
    assert started["args"] == ("trip123",)
    assert started["daemon"] is True
    assert started["started"] is True


def test_enqueue_photos_kind_dispatches_to_run_photos(monkeypatch):
    started = {}

    class FakeThread:
        def __init__(self, target, args, daemon):
            started["target"] = target

        def start(self):
            pass

    monkeypatch.setattr(tasks.threading, "Thread", FakeThread)

    import webapp

    monkeypatch.setattr(webapp, "run_photos", lambda tid: None)
    tasks.enqueue("photos", "trip123")

    assert started["target"] is webapp.run_photos


def _install_fake_tasks_v2(monkeypatch, captured):
    class FakeClient:
        def create_task(self, parent, task):
            captured["parent"] = parent
            captured["task"] = task

    fake_module = types.SimpleNamespace(
        CloudTasksClient=FakeClient,
        HttpMethod=SimpleNamespace(POST="POST"),
    )
    fake_google_cloud = types.ModuleType("google.cloud")
    fake_google_cloud.tasks_v2 = fake_module
    monkeypatch.setitem(sys.modules, "google.cloud", fake_google_cloud)
    monkeypatch.setitem(sys.modules, "google.cloud.tasks_v2", fake_module)


def test_enqueue_with_queue_configured_creates_a_cloud_task(monkeypatch):
    monkeypatch.setattr(config, "CLOUD_TASKS_QUEUE", "projects/p/locations/l/queues/q")
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "https://memotrip.app")
    monkeypatch.setattr(config, "TASKS_INVOKER_SA", "invoker@p.iam.gserviceaccount.com")
    captured = {}
    _install_fake_tasks_v2(monkeypatch, captured)

    tasks.enqueue("build", "trip123")

    assert captured["parent"] == "projects/p/locations/l/queues/q"
    req = captured["task"]["http_request"]
    assert req["url"] == "https://memotrip.app/internal/tasks/build/trip123"
    assert req["oidc_token"]["audience"] == req["url"]
    assert req["oidc_token"]["service_account_email"] == "invoker@p.iam.gserviceaccount.com"


def test_enqueue_with_queue_configured_never_starts_a_local_thread(monkeypatch):
    monkeypatch.setattr(config, "CLOUD_TASKS_QUEUE", "projects/p/locations/l/queues/q")
    captured = {}
    _install_fake_tasks_v2(monkeypatch, captured)

    def _boom(*a, **k):
        raise AssertionError("threading.Thread should not be used when a queue is configured")

    monkeypatch.setattr(tasks.threading, "Thread", _boom)
    tasks.enqueue("photos", "trip123")

    assert captured["task"]["http_request"]["url"].endswith("/internal/tasks/photos/trip123")
