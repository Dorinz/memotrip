"""Unit tests for fetch_photos.py's per-user (web app) OAuth token handling
and the download() filename sanitization.

Every test here uses the sqlite test.db fixture from conftest.py for the
photo_accounts table, and mocks google.oauth2.credentials.Credentials /
google.auth.transport.requests.Request so no real Google endpoint is ever
contacted.
"""

from __future__ import annotations

import datetime
import json
import time
from types import SimpleNamespace

import pytest
from google.auth.exceptions import RefreshError

import db
import fetch_photos as fp


@pytest.fixture()
def photo_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "test.db")
    db.init_schema()
    return db


def _insert_account(owner_key, refresh_token="rt", access_token="at", expiry=None):
    with db.db() as c:
        c.execute(
            "INSERT INTO photo_accounts(owner_key, refresh_token, access_token, "
            "token_expiry, granted) VALUES(?,?,?,?,?)",
            (owner_key, refresh_token, access_token, expiry, "2026-01-01 00:00"),
        )


def _fake_web_client_info(monkeypatch):
    monkeypatch.setattr(fp, "_web_client_info", lambda path: ("client-id", "client-secret"))


# --------------------------------------------------------------- authorise_for_user


def test_authorise_for_user_raises_when_never_connected(photo_db):
    with pytest.raises(fp.PhotosNotConnected):
        fp.authorise_for_user("user:1", "creds.json")


def test_authorise_for_user_raises_when_row_has_no_refresh_token(photo_db):
    _insert_account("user:1", refresh_token=None)
    with pytest.raises(fp.PhotosNotConnected):
        fp.authorise_for_user("user:1", "creds.json")


def test_authorise_for_user_returns_session_when_token_is_valid(photo_db, monkeypatch):
    _insert_account("user:1")
    _fake_web_client_info(monkeypatch)

    class ValidCreds:
        valid = True
        token = "at"
        refresh_token = "rt"

    monkeypatch.setattr(fp, "Credentials", lambda **kw: ValidCreds())
    session = fp.authorise_for_user("user:1", "creds.json")
    assert session is not None


def test_authorise_for_user_refreshes_an_expired_token_and_saves_it(photo_db, monkeypatch):
    _insert_account("user:1")
    _fake_web_client_info(monkeypatch)
    saved = {}

    class ExpiredCreds:
        valid = False
        token = "old-at"
        refresh_token = "rt"
        expiry = None

        def refresh(self, request):
            self.valid = True
            self.token = "new-at"

    monkeypatch.setattr(fp, "Credentials", lambda **kw: ExpiredCreds())
    monkeypatch.setattr(fp, "save_user_credentials", lambda owner, creds: saved.update(owner=owner))
    fp.authorise_for_user("user:1", "creds.json")
    assert saved["owner"] == "user:1"


def test_authorise_for_user_drops_row_when_refresh_token_is_revoked(photo_db, monkeypatch):
    _insert_account("user:1")
    _fake_web_client_info(monkeypatch)

    class DeadCreds:
        valid = False
        token = "old-at"
        refresh_token = "rt"
        expiry = None

        def refresh(self, request):
            raise RefreshError("invalid_grant")

    monkeypatch.setattr(fp, "Credentials", lambda **kw: DeadCreds())
    with pytest.raises(fp.PhotosNotConnected):
        fp.authorise_for_user("user:1", "creds.json")

    with db.db() as c:
        row = c.execute("SELECT * FROM photo_accounts WHERE owner_key=?", ("user:1",)).fetchone()
    assert row is None


def test_authorise_for_user_parses_stored_expiry(photo_db, monkeypatch):
    _insert_account("user:1", expiry="2030-01-01T00:00:00")
    _fake_web_client_info(monkeypatch)
    captured = {}

    class ValidCreds:
        valid = True

        def __init__(self, **kw):
            captured.update(kw)

    monkeypatch.setattr(fp, "Credentials", ValidCreds)
    fp.authorise_for_user("user:1", "creds.json")
    assert captured["expiry"] == datetime.datetime(2030, 1, 1, 0, 0, 0)


# ------------------------------------------------------------- open_session_for_user


def test_open_session_for_user_wraps_a_mid_request_refresh_error(photo_db, monkeypatch):
    _insert_account("user:1")

    class ValidCreds:
        valid = True

    monkeypatch.setattr(fp, "_web_client_info", lambda path: ("cid", "secret"))
    monkeypatch.setattr(fp, "Credentials", lambda **kw: ValidCreds())

    class FakeSession:
        def post(self, url, json):
            raise RefreshError("revoked mid-request")

    monkeypatch.setattr(fp, "AuthorizedSession", lambda creds: FakeSession())

    with pytest.raises(fp.PhotosNotConnected):
        fp.open_session_for_user("user:1", "creds.json")

    with db.db() as c:
        row = c.execute("SELECT * FROM photo_accounts WHERE owner_key=?", ("user:1",)).fetchone()
    assert row is None


# ------------------------------------------------------------- migrate_guest_connection


def test_migrate_guest_connection_noop_when_guest_never_connected(photo_db):
    fp.migrate_guest_connection("g1", "user:1")  # must not raise
    with db.db() as c:
        rows = c.execute("SELECT * FROM photo_accounts").fetchall()
    assert rows == []


def test_migrate_guest_connection_noop_when_guest_id_is_empty(photo_db):
    _insert_account("guest:g1")
    fp.migrate_guest_connection("", "user:1")
    with db.db() as c:
        rows = c.execute("SELECT owner_key FROM photo_accounts").fetchall()
    assert [r["owner_key"] for r in rows] == ["guest:g1"]


def test_migrate_guest_connection_rekeys_when_user_has_no_own_connection(photo_db):
    _insert_account("guest:g1", refresh_token="guest-token")
    fp.migrate_guest_connection("g1", "user:1")
    with db.db() as c:
        rows = c.execute("SELECT * FROM photo_accounts").fetchall()
    assert len(rows) == 1
    assert rows[0]["owner_key"] == "user:1"
    assert rows[0]["refresh_token"] == "guest-token"


def test_migrate_guest_connection_drops_guest_row_when_user_already_connected(photo_db):
    _insert_account("guest:g1", refresh_token="guest-token")
    _insert_account("user:1", refresh_token="user-own-token")
    fp.migrate_guest_connection("g1", "user:1")
    with db.db() as c:
        rows = {
            r["owner_key"]: r["refresh_token"] for r in c.execute("SELECT * FROM photo_accounts")
        }
    assert rows == {"user:1": "user-own-token"}


# ------------------------------------------------------------------- save_user_credentials


def test_save_user_credentials_inserts_a_new_row(photo_db):
    creds = SimpleNamespace(refresh_token="rt", token="at", expiry=None)
    fp.save_user_credentials("user:1", creds)
    with db.db() as c:
        row = c.execute("SELECT * FROM photo_accounts WHERE owner_key=?", ("user:1",)).fetchone()
    assert row["refresh_token"] == "rt"
    assert row["access_token"] == "at"


def test_save_user_credentials_upserts_an_existing_row(photo_db):
    _insert_account("user:1", refresh_token="old-rt", access_token="old-at")
    creds = SimpleNamespace(refresh_token="new-rt", token="new-at", expiry=None)
    fp.save_user_credentials("user:1", creds)
    with db.db() as c:
        rows = c.execute("SELECT * FROM photo_accounts").fetchall()
    assert len(rows) == 1
    assert rows[0]["refresh_token"] == "new-rt"
    assert rows[0]["access_token"] == "new-at"


# ------------------------------------------------------------------------------ download


def test_download_sanitizes_unsafe_filename_characters(tmp_path, monkeypatch):
    class FakeResponse:
        content = b"fake-jpeg-bytes"
        ok = True

    class FakeHttp:
        def get(self, url):
            return FakeResponse()

    monkeypatch.setattr(fp, "_ok", lambda r: r)
    items = [
        {
            "type": "PHOTO",
            "id": "abc123",
            "createTime": "2026-08-01T10:00:00Z",
            "mediaFile": {
                "baseUrl": "https://example.com/img",
                "filename": 'weird/name:*?"<>|.jpg',
                "mediaFileMetadata": {"width": "10", "height": "20"},
            },
        }
    ]
    manifest = fp.download(FakeHttp(), items, tmp_path, 1600)
    assert len(manifest) == 1
    saved_name = manifest[0]["file"]
    assert (tmp_path / saved_name).exists()
    for bad_char in '/:*?"<>|':
        assert bad_char not in saved_name


def test_download_disambiguates_duplicate_filenames(tmp_path, monkeypatch):
    class FakeResponse:
        content = b"bytes"
        ok = True

    class FakeHttp:
        def get(self, url):
            return FakeResponse()

    monkeypatch.setattr(fp, "_ok", lambda r: r)

    def _item(id_):
        return {
            "type": "PHOTO",
            "id": id_,
            "createTime": "",
            "mediaFile": {
                "baseUrl": "https://example.com/img",
                "filename": "IMG_0001.jpg",
                "mediaFileMetadata": {},
            },
        }

    manifest = fp.download(FakeHttp(), [_item("a"), _item("b")], tmp_path, 1600)
    names = {m["file"] for m in manifest}
    assert len(names) == 2


def test_download_skips_non_photo_items(tmp_path, monkeypatch):
    monkeypatch.setattr(fp, "_ok", lambda r: r)

    class FakeHttp:
        def get(self, url):
            raise AssertionError("should not download a non-PHOTO item")

    manifest = fp.download(FakeHttp(), [{"type": "VIDEO", "id": "v1"}], tmp_path, 1600)
    assert manifest == []


def test_download_writes_a_manifest_file(tmp_path, monkeypatch):
    class FakeResponse:
        content = b"x"
        ok = True

    class FakeHttp:
        def get(self, url):
            return FakeResponse()

    monkeypatch.setattr(fp, "_ok", lambda r: r)
    item = {
        "type": "PHOTO",
        "id": "a",
        "createTime": "2026-08-01T10:00:00Z",
        "mediaFile": {"baseUrl": "https://x", "filename": "a.jpg", "mediaFileMetadata": {}},
    }
    fp.download(FakeHttp(), [item], tmp_path, 800)
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["downscale_px"] == 800
    assert manifest["count"] == 1


def _photo_item(id_, url=None):
    return {
        "type": "PHOTO",
        "id": id_,
        "createTime": "2026-08-01T10:00:00Z",
        "mediaFile": {
            "baseUrl": url or f"https://example.com/{id_}",
            "filename": f"{id_}.jpg",
            "mediaFileMetadata": {},
        },
    }


def test_download_runs_in_parallel_and_keeps_pick_order(tmp_path, monkeypatch):
    import threading

    monkeypatch.setattr(fp, "_ok", lambda r: r)
    lock = threading.Lock()
    state = {"now": 0, "peak": 0}

    class FakeHttp:
        def get(self, url):
            with lock:
                state["now"] += 1
                state["peak"] = max(state["peak"], state["now"])
            time.sleep(0.05)
            with lock:
                state["now"] -= 1
            return SimpleNamespace(content=url.encode(), ok=True)

    items = [_photo_item(f"p{i}") for i in range(16)]
    manifest = fp.download(FakeHttp(), items, tmp_path, 1600, workers=8)
    assert [m["id"] for m in manifest] == [f"p{i}" for i in range(16)]
    assert state["peak"] > 1
    assert (tmp_path / "p3.jpg").read_bytes() == b"https://example.com/p3=w1600-h1600"


def test_download_retries_a_transient_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(fp, "_ok", lambda r: r)
    monkeypatch.setattr(fp.time, "sleep", lambda s: None)
    calls = {"n": 0}

    class FakeHttp:
        def get(self, url):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ConnectionError("reset")
            return SimpleNamespace(content=b"ok", ok=True)

    manifest = fp.download(FakeHttp(), [_photo_item("a")], tmp_path, 1600)
    assert len(manifest) == 1
    assert calls["n"] == 2


def test_download_skips_a_photo_that_keeps_failing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(fp, "_ok", lambda r: r)
    monkeypatch.setattr(fp.time, "sleep", lambda s: None)

    class FakeHttp:
        def get(self, url):
            if "bad" in url:
                raise ConnectionError("down")
            return SimpleNamespace(content=b"ok", ok=True)

    items = [_photo_item("good1"), _photo_item("bad"), _photo_item("good2")]
    manifest = fp.download(FakeHttp(), items, tmp_path, 1600)
    assert [m["id"] for m in manifest] == ["good1", "good2"]
    saved = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert saved["count"] == 2
    assert "1 photo(s) could not be downloaded" in capsys.readouterr().err


# ----------------------------------------------------------------- two-size download


def test_download_keeps_each_photos_base_url_for_a_later_upgrade(tmp_path, monkeypatch):
    monkeypatch.setattr(fp, "_ok", lambda r: r)

    class FakeHttp:
        def get(self, url):
            return SimpleNamespace(content=b"x", ok=True)

    manifest = fp.download(FakeHttp(), [_photo_item("a")], tmp_path, 512)
    assert manifest[0]["baseUrl"] == "https://example.com/a"


def test_collect_keep_session_leaves_the_picker_session_open(tmp_path, monkeypatch):
    deleted = []
    monkeypatch.setattr(fp, "list_items", lambda http, sid: [])
    monkeypatch.setattr(fp, "close_session", lambda http, sid: deleted.append(sid))
    fp.collect(object(), "s1", tmp_path, 512, keep_session=True)
    assert deleted == []
    fp.collect(object(), "s2", tmp_path, 512)
    assert deleted == ["s2"]


def _manifest_with(tmp_path, *names):
    items = [{"file": n, "baseUrl": f"https://example.com/{n}"} for n in names]
    (tmp_path / "manifest.json").write_text(
        json.dumps({"downscale_px": 512, "items": items}), encoding="utf-8"
    )


def test_upgrade_fetches_only_the_requested_photos_at_page_size(tmp_path, monkeypatch):
    monkeypatch.setattr(fp, "_ok", lambda r: r)
    urls = []

    class FakeHttp:
        def get(self, url):
            urls.append(url)
            return SimpleNamespace(content=b"big", ok=True)

    _manifest_with(tmp_path, "a.jpg", "b.jpg", "c.jpg")
    got = fp.upgrade(FakeHttp(), tmp_path, ["a.jpg", "c.jpg"], 1600)
    assert sorted(urls) == [
        "https://example.com/a.jpg=w1600-h1600",
        "https://example.com/c.jpg=w1600-h1600",
    ]
    assert got == {"a.jpg": tmp_path / "full" / "a.jpg", "c.jpg": tmp_path / "full" / "c.jpg"}
    saved = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert [it.get("full_file") for it in saved["items"]] == ["full/a.jpg", None, "full/c.jpg"]


def test_upgrade_does_not_fetch_a_photo_twice(tmp_path, monkeypatch):
    monkeypatch.setattr(fp, "_ok", lambda r: r)
    calls = {"n": 0}

    class FakeHttp:
        def get(self, url):
            calls["n"] += 1
            return SimpleNamespace(content=b"big", ok=True)

    _manifest_with(tmp_path, "a.jpg")
    fp.upgrade(FakeHttp(), tmp_path, ["a.jpg"])
    got = fp.upgrade(FakeHttp(), tmp_path, ["a.jpg"])
    assert calls["n"] == 1
    assert "a.jpg" in got


def test_upgrade_leaves_out_a_photo_that_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(fp, "_ok", lambda r: r)
    monkeypatch.setattr(fp.time, "sleep", lambda s: None)

    class FakeHttp:
        def get(self, url):
            if "bad" in url:
                raise ConnectionError("expired")
            return SimpleNamespace(content=b"big", ok=True)

    _manifest_with(tmp_path, "ok.jpg", "bad.jpg")
    got = fp.upgrade(FakeHttp(), tmp_path, ["ok.jpg", "bad.jpg"])
    assert list(got) == ["ok.jpg"]
