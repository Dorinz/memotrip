"""What of a trip's folder is public, how long the downloaded album is kept, and
that a rerun keeps the page's photos once that album is gone."""

from __future__ import annotations

import json
import os
import time

import pytest

import webapp

TID = "a" * 32


@pytest.fixture()
def trip(client, tmp_path, monkeypatch):
    """A trip folder with everything a real one holds, under a temp DATA dir."""
    monkeypatch.setattr(webapp, "DATA", tmp_path / "data")
    d = tmp_path / "data" / TID
    for sub in ("docs", "gphotos/full", "images/trips", "images/lodging"):
        (d / sub).mkdir(parents=True)
    (d / "page.html").write_text("<html>page</html>", encoding="utf-8")
    (d / "spec.json").write_text("{}", encoding="utf-8")
    (d / "docs" / "bookings.pdf").write_bytes(b"secret codes")
    (d / "gphotos" / "manifest.json").write_text("{}", encoding="utf-8")
    (d / "gphotos" / "IMG_1.jpg").write_bytes(b"album photo")
    (d / "gphotos" / "full" / "IMG_1.jpg").write_bytes(b"album photo big")
    (d / "images" / "trips" / "day-d1-1.jpg").write_bytes(b"page photo")
    (d / "images" / "lodging" / "home-1.jpg").write_bytes(b"old lodging photo")
    return d


# --------------------------------------------------------------------------- privacy


def test_the_finished_page_is_public(client, trip):
    r = client.get(f"/data/{TID}/page.html")
    assert r.status_code == 200
    assert r.text == "<html>page</html>"


def test_the_photos_shown_on_the_page_are_public(client, trip):
    assert client.get(f"/data/{TID}/images/trips/day-d1-1.jpg").content == b"page photo"
    assert client.get(f"/data/{TID}/images/lodging/home-1.jpg").status_code == 200


@pytest.mark.parametrize(
    "path",
    [
        "docs/bookings.pdf",
        "gphotos/IMG_1.jpg",
        "gphotos/full/IMG_1.jpg",
        "gphotos/manifest.json",
        "spec.json",
        "images/other/x.jpg",
        "images/trips/../../spec.json",
        "images/trips/%2e%2e%2fspec.json",
    ],
)
def test_nothing_else_in_the_trip_folder_is_public(client, trip, path):
    assert client.get(f"/data/{TID}/{path}").status_code == 404


def test_a_malformed_trip_id_is_not_found(client, trip):
    assert client.get("/data/..%2f..%2fwebapp.py/page.html").status_code == 404
    assert client.get("/data/notahexid/page.html").status_code == 404


# ------------------------------------------------------------------------- retention


def _age(path, days):
    t = time.time() - days * 86400
    os.utime(path, (t, t))


def test_purge_removes_an_album_older_than_the_retention_period(trip):
    _age(trip / "gphotos" / "manifest.json", webapp.PHOTO_RETENTION_DAYS + 1)
    assert webapp.purge_expired_photos(log=lambda m: None) == [TID]
    assert not (trip / "gphotos").exists()
    # the page and the photos it shows are separate files and stay
    assert (trip / "page.html").is_file()
    assert (trip / "images" / "trips" / "day-d1-1.jpg").is_file()
    assert (trip / "docs" / "bookings.pdf").is_file()


def test_purge_keeps_a_recent_album(trip):
    _age(trip / "gphotos" / "manifest.json", webapp.PHOTO_RETENTION_DAYS - 1)
    assert webapp.purge_expired_photos(log=lambda m: None) == []
    assert (trip / "gphotos" / "full" / "IMG_1.jpg").is_file()


def test_purge_ignores_folders_that_are_not_trips(trip):
    other = webapp.DATA / "not-a-trip" / "gphotos"
    other.mkdir(parents=True)
    (other / "manifest.json").write_text("{}", encoding="utf-8")
    _age(other / "manifest.json", 365)
    assert webapp.purge_expired_photos(log=lambda m: None) == []
    assert other.is_dir()


# ----------------------------------------------------------- rerun after the purge


def test_rerun_without_an_album_keeps_the_pages_photos(trip):
    (trip / "images" / "trips" / "hero-1.jpg").write_bytes(b"hero")
    old = {
        "timeline": [
            {
                "type": "day",
                "key": "day",
                "date": "2026-06-22",
                "tripPhotos": [
                    "images/trips/day-d1-1.jpg",
                    "images/trips/gone.jpg",
                ],
            },
        ],
        "photos": {"stop": {"lodging": [], "trip": ["images/trips/day-d1-1.jpg"]}},
        "hero": {"photos": ["images/trips/hero-1.jpg"]},
    }
    (trip / "spec.json").write_text(json.dumps(old), encoding="utf-8")
    new = {
        "timeline": [
            {"type": "day", "key": "renamed", "date": "2026-06-22"},
            {"type": "day", "key": "renamed", "date": "2026-06-23"},
        ],
        "photos": {"stop": {"lodging": [], "trip": []}},
        "hero": {"h1": "fresh copy"},
    }
    kept = webapp._carry_over_page_photos(TID, new)
    # matched by date, not by key; a path whose file is gone is dropped
    assert new["timeline"][0]["tripPhotos"] == ["images/trips/day-d1-1.jpg"]
    assert "tripPhotos" not in new["timeline"][1]
    assert new["photos"]["stop"]["trip"] == ["images/trips/day-d1-1.jpg"]
    assert new["hero"] == {"h1": "fresh copy", "photos": ["images/trips/hero-1.jpg"]}
    assert kept == 3


def test_rerun_of_a_trip_with_no_previous_spec_keeps_nothing(trip):
    (trip / "spec.json").unlink()
    assert webapp._carry_over_page_photos(TID, {"timeline": []}) == 0
