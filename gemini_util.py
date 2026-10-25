"""Shared Gemini helpers: one client, JSON calls with retry/backoff on the
transient errors the API throws under load (429 / 500 / 503 / UNAVAILABLE),
and an optional immediate retry when the answer itself is unusable."""

from __future__ import annotations

import json
import logging
import os
import time

_TRANSIENT = (
    "429",
    "500",
    "503",
    "unavailable",
    "resource_exhausted",
    "resourceexhausted",
    "overloaded",
    "high demand",
    "internal error",
)


def have_key() -> str | None:
    return os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")


def client(api_key: str | None = None):
    key = api_key or have_key()
    if not key:
        raise RuntimeError("no GEMINI_API_KEY / GOOGLE_API_KEY in the environment")
    from google import genai

    logging.getLogger("google_genai").setLevel(logging.ERROR)
    return genai.Client(api_key=key)


class BadResponse(RuntimeError):
    """The model answered, but the answer is unusable: cut off at the output
    token limit, not valid JSON, or rejected by the caller's `check`."""


def _finish_reason(resp) -> str | None:
    try:
        fr = resp.candidates[0].finish_reason
    except (AttributeError, IndexError, TypeError):
        return None
    return getattr(fr, "name", None) or (str(fr) if fr is not None else None)


def _usage_line(model: str, resp, finish: str | None, secs: float) -> str | None:
    u = getattr(resp, "usage_metadata", None)
    if u is None:
        return None
    return (
        f"  gemini {model}: {finish or '?'} · {u.candidates_token_count or 0} out"
        f" + {getattr(u, 'thoughts_token_count', None) or 0} thinking tokens · {secs:.0f}s"
    )


def generate_json(
    model: str,
    contents,
    *,
    schema: dict | None = None,
    retries: int = 5,
    log=print,
    cl=None,
    max_output_tokens: int | None = None,
    check=None,
    bad_retries: int = 0,
) -> dict:
    """Call the model, expect JSON back, parse it. Retries transient failures
    (with backoff). Every answer's finish reason and token usage is logged.

    Now and then the model gets stuck repeating itself inside a string and
    runs to the token cap (broken JSON), or bails out by closing the JSON
    early (valid but nearly empty). Such an answer - cut off at the cap, not
    valid JSON, or one `check(data)` rejects by returning a reason - is asked
    for again right away, up to `bad_retries` more times, then BadResponse."""
    from google.genai import types

    cl = cl or client()
    cfg = types.GenerateContentConfig(
        response_mime_type="application/json",
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        **({"response_schema": schema} if schema else {}),
        **({"max_output_tokens": max_output_tokens} if max_output_tokens else {}),
    )
    delay = 3.0
    fails = bad = 0
    while True:
        t0 = time.time()
        try:
            resp = cl.models.generate_content(model=model, contents=contents, config=cfg)
        except Exception as e:
            fails += 1
            msg = str(e).lower()
            transient = any(t in msg for t in _TRANSIENT)
            if not transient or fails >= retries:
                raise
            log(f"  gemini {type(e).__name__} (attempt {fails}/{retries}) — retry in {delay:.0f}s")
            time.sleep(delay)
            delay = min(delay * 2, 45)
            continue

        finish = _finish_reason(resp)
        line = _usage_line(model, resp, finish, time.time() - t0)
        if line:
            log(line)
        if finish == "MAX_TOKENS":
            problem = "cut off at the output token limit"
        else:
            try:
                data = json.loads(resp.text)
            except (TypeError, ValueError) as e:
                problem = f"not valid JSON ({e})"
            else:
                problem = check(data) if check else None
                if not problem:
                    return data
        bad += 1
        if bad > bad_retries:
            raise BadResponse(problem)
        log(f"  gemini answer unusable: {problem} — asking again ({bad}/{bad_retries})")
