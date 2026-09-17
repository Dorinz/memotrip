#!/usr/bin/env python3
"""tasks.py — background pipeline dispatch for the trip-build/photos steps.

Production (CLOUD_TASKS_QUEUE set): enqueues an HTTP task on Cloud Tasks,
which calls back into this same service's /internal/tasks/<kind>/<tid> route
carrying an OIDC token Cloud Run can verify. This lets Cloud Run scale to
zero between trips instead of paying for an always-on instance babysitting
threads.

Local dev (CLOUD_TASKS_QUEUE unset): falls back to exactly the old
in-process threading.Thread behavior — no GCP infra required to develop.
"""
from __future__ import annotations

import threading

import config


def enqueue(kind: str, tid: str) -> None:
    """kind is "build" or "photos"."""
    if config.CLOUD_TASKS_QUEUE:
        _enqueue_cloud_task(kind, tid)
        return
    import webapp  # local import — avoids a circular import at module load time
    target = webapp.run_build if kind == "build" else webapp.run_photos
    threading.Thread(target=target, args=(tid,), daemon=True).start()


def _enqueue_cloud_task(kind: str, tid: str) -> None:
    from google.cloud import tasks_v2  # lazy import — only needed in prod mode

    client = tasks_v2.CloudTasksClient()
    url = f"{config.PUBLIC_BASE_URL}/internal/tasks/{kind}/{tid}"
    task = {
        "http_request": {
            "http_method": tasks_v2.HttpMethod.POST,
            "url": url,
            "oidc_token": {
                "service_account_email": config.TASKS_INVOKER_SA,
                "audience": url,
            },
        }
    }
    # CLOUD_TASKS_QUEUE must be the full queue resource name:
    # projects/<project>/locations/<region>/queues/<queue>
    client.create_task(parent=config.CLOUD_TASKS_QUEUE, task=task)
