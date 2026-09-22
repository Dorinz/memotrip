"""Import for its side effect: load KEY=VALUE lines from a local .env into os.environ
(without overriding anything already set). Keeps the Gemini key with the project
instead of in a shell session. No dependency."""

import os
import pathlib

_env = pathlib.Path(__file__).resolve().parent / ".env"
if _env.is_file():
    for _line in _env.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _k, _, _v = _line.partition("=")
        _k, _v = _k.strip(), _v.strip().strip('"').strip("'")
        os.environ.setdefault(_k, _v)
