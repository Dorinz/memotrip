#!/usr/bin/env python3
"""config.py — small env-var reads for prod/dev switches, no settings framework
(consistent with this codebase's existing style — see local_env.py and the
SESSION_SECRET fallback in webapp.py).

    PUBLIC_BASE_URL     this service's own externally-reachable base URL —
                        used to build OAuth redirect URIs and Cloud Tasks
                        callback URLs. Defaults to localhost for local dev.
    DATABASE_URL        set -> db.py uses Postgres (Cloud SQL in prod).
                        unset -> db.py uses the local trips.db sqlite file.
    CLOUD_TASKS_QUEUE   set -> tasks.py enqueues via Cloud Tasks. Must be the
                        full queue resource name:
                        projects/<project>/locations/<region>/queues/<queue>
                        unset -> tasks.py falls back to a plain
                        threading.Thread, exactly like the old behavior.
    TASKS_INVOKER_SA    service account email Cloud Tasks signs its OIDC
                        token with — webapp.py checks incoming
                        /internal/tasks/* requests carry a token from this
                        identity.
    OAUTH_WEB_CLIENT_PATH
                        path to the Web-application OAuth client JSON used
                        for per-user Google Photos sign-in. Defaults to
                        credentials_web.json next to this file (local dev).
                        In prod this is mounted from Secret Manager into a
                        dedicated directory, NOT under /app directly — Cloud
                        Run mounting a secret file there would shadow the
                        whole source tree and crash the container.
    RESEND_API_KEY      Resend (resend.com) API key for sending the
                        password-reset email - see mailer.py. Unset -> the
                        email is printed to the server log instead of sent,
                        so local dev works without a Resend account.
"""

import os
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent

PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000")
DATABASE_URL = os.environ.get("DATABASE_URL")
CLOUD_TASKS_QUEUE = os.environ.get("CLOUD_TASKS_QUEUE")
TASKS_INVOKER_SA = os.environ.get("TASKS_INVOKER_SA")
OAUTH_WEB_CLIENT_PATH = pathlib.Path(
    os.environ.get("OAUTH_WEB_CLIENT_PATH", str(ROOT / "credentials_web.json"))
)
RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
