"""Unit tests for local_env.py's .env parsing (a module-level side effect).

local_env.py has no functions to call directly - it parses .env as top-level
code, relative to its own __file__. Each test runs a temp copy of that source
via runpy (which sets __file__ to the copy's real path) next to a throwaway
.env, so no test can leak a real key from the project's own .env into another
test via os.environ, and importlib.reload's __file__-from-spec behavior never
comes into play.
"""

from __future__ import annotations

import os
import pathlib
import runpy

import pytest

SOURCE = (pathlib.Path(__file__).resolve().parent.parent / "local_env.py").read_text(
    encoding="utf-8"
)


def _run_local_env(tmp_path, env_text: str | None) -> None:
    if env_text is not None:
        (tmp_path / ".env").write_text(env_text, encoding="utf-8")
    copy_path = tmp_path / "local_env.py"
    copy_path.write_text(SOURCE, encoding="utf-8")
    runpy.run_path(str(copy_path))


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in ("FOO", "BAR", "BAZ"):
        monkeypatch.delenv(key, raising=False)


def test_loads_simple_key_value(tmp_path):
    _run_local_env(tmp_path, "FOO=bar\n")
    assert os.environ["FOO"] == "bar"


def test_skips_comments_and_blank_lines(tmp_path):
    _run_local_env(tmp_path, "# a comment\n\nFOO=bar\n")
    assert os.environ["FOO"] == "bar"


def test_skips_lines_without_equals(tmp_path):
    _run_local_env(tmp_path, "not a key value line\nFOO=bar\n")
    assert os.environ["FOO"] == "bar"


def test_strips_surrounding_quotes(tmp_path):
    _run_local_env(tmp_path, "FOO=\"bar\"\nBAR='baz'\n")
    assert os.environ["FOO"] == "bar"
    assert os.environ["BAR"] == "baz"


def test_strips_surrounding_whitespace(tmp_path):
    _run_local_env(tmp_path, "  FOO  =  bar  \n")
    assert os.environ["FOO"] == "bar"


def test_does_not_override_existing_env_var(tmp_path, monkeypatch):
    monkeypatch.setenv("FOO", "already-set")
    _run_local_env(tmp_path, "FOO=from-dotenv\n")
    assert os.environ["FOO"] == "already-set"


def test_missing_env_file_is_a_silent_noop(tmp_path):
    _run_local_env(tmp_path, None)  # no .env written in tmp_path
    assert "FOO" not in os.environ


def test_value_containing_equals_sign_is_kept_whole(tmp_path):
    _run_local_env(tmp_path, "FOO=a=b=c\n")
    assert os.environ["FOO"] == "a=b=c"
