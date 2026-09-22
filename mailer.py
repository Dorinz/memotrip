#!/usr/bin/env python3
"""mailer.py — outbound transactional email via Resend's REST API.

RESEND_API_KEY unset (e.g. local dev, before signing up for Resend) ->
the email is printed to stdout instead of sent, so password-reset (the
only feature that needs this) stays testable without a real account.
"""

from __future__ import annotations

import requests

import config

FROM_ADDRESS = "MemoTrip <noreply@memotrip.app>"


def send(to: str, subject: str, html: str) -> None:
    if not config.RESEND_API_KEY:
        print(
            f"[mailer] RESEND_API_KEY not set - not sending. Would have emailed {to}:\n"
            f"  subject: {subject}\n  {html}"
        )
        return
    r = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {config.RESEND_API_KEY}"},
        json={"from": FROM_ADDRESS, "to": [to], "subject": subject, "html": html},
        timeout=10,
    )
    if not r.ok:
        # Resend's error body says *why* (e.g. the sending domain isn't
        # verified yet) - surface it before raising, or the caller only ever
        # sees a bare "422 Client Error" with no way to tell what's wrong.
        print(f"[mailer] Resend {r.status_code} sending to {to}: {r.text[:500]}")
    r.raise_for_status()
