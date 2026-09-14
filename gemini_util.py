"""Shared Gemini helpers: one client, JSON calls with retry/backoff on the
transient errors the API throws under load (429 / 500 / 503 / UNAVAILABLE)."""
from __future__ import annotations

import json
import logging
import os
import time

_TRANSIENT = ("429", "500", "503", "unavailable", "resource_exhausted",
              "resourceexhausted", "overloaded", "high demand", "internal error")


def have_key() -> str | None:
    return os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")


def client(api_key: str | None = None):
    key = api_key or have_key()
    if not key:
        raise RuntimeError("no GEMINI_API_KEY / GOOGLE_API_KEY in the environment")
    from google import genai
    logging.getLogger("google_genai").setLevel(logging.ERROR)
    return genai.Client(api_key=key)


def generate_json(model: str, contents, *, schema: dict | None = None,
                  temperature: float = 0.4, retries: int = 5, log=print,
                  cl=None) -> dict:
    """Call the model, expect JSON back, parse it. Retries transient failures."""
    from google.genai import types
    cl = cl or client()
    cfg = types.GenerateContentConfig(
        response_mime_type="application/json", temperature=temperature,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        **({"response_schema": schema} if schema else {}))
    delay = 3.0
    last = None
    for attempt in range(1, retries + 1):
        try:
            resp = cl.models.generate_content(model=model, contents=contents, config=cfg)
            return json.loads(resp.text)
        except Exception as e:
            last = e
            msg = str(e).lower()
            transient = any(t in msg for t in _TRANSIENT)
            if not transient or attempt == retries:
                raise
            log(f"  gemini {type(e).__name__} (attempt {attempt}/{retries}) — retry in {delay:.0f}s")
            time.sleep(delay)
            delay = min(delay * 2, 45)
    raise last
