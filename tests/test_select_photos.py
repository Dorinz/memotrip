"""Unit tests for select_photos.py: image analysis, calendar-day bucketing,
de-dup/diversity picking, and the end-to-end select() pipeline.

Gemini-backed picking (pick_with_gemini, pick_hero_with_gemini) is exercised
only through use_ai=False / a missing API key in select() tests here - the
offline (score + dHash) path is what's actually deterministic and testable
without a network call.
"""

from __future__ import annotations

import datetime as dt
import json
import random

import pytest
from PIL import Image

import select_photos as sp

# ------------------------------------------------------------------------- helpers


def _save_jpeg(path, im, exif_dt: str | None = None, exif_offset: str | None = None):
    kwargs = {}
    if exif_dt is not None:
        exif = Image.Exif()
        exif[sp.EXIF_DATETIME_ORIGINAL] = exif_dt
        if exif_offset is not None:
            exif[sp.EXIF_OFFSET_TIME_ORIGINAL] = exif_offset
        kwargs["exif"] = exif.tobytes()
    im.save(path, "JPEG", **kwargs)


def _noisy_gray(size=64, seed=0):
    rnd = random.Random(seed)
    im = Image.new("L", (size, size))
    im.putdata([rnd.randint(0, 255) for _ in range(size * size)])
    return im


def make_photo_file(tmp_path, name, *, seed=0, color=None, exif_dt=None, exif_offset=None, size=64):
    """Writes a real JPEG to disk: a noisy (sharp) grayscale image by default,
    or a flat color image when `color` is given (for blur/exposure tests)."""
    im = Image.new("RGB", (size, size), color) if color else _noisy_gray(size, seed).convert("RGB")
    path = tmp_path / name
    _save_jpeg(path, im, exif_dt, exif_offset)
    return path


# --------------------------------------------------------------------------- _parse_iso


def test_parse_iso_with_z_suffix():
    d = sp._parse_iso("2026-08-03T10:00:00Z")
    assert d == dt.datetime(2026, 8, 3, 10, 0, 0)


def test_parse_iso_with_offset_converts_to_utc():
    d = sp._parse_iso("2026-08-03T12:00:00+02:00")
    assert d == dt.datetime(2026, 8, 3, 10, 0, 0)


def test_parse_iso_empty_string_returns_none():
    assert sp._parse_iso("") is None


def test_parse_iso_garbage_returns_none():
    assert sp._parse_iso("not a date") is None


# ------------------------------------------------------------------------------ analysis


def test_sharpness_of_noisy_image_exceeds_flat_image():
    noisy = _noisy_gray()
    flat = Image.new("L", (64, 64), 128)
    assert sp.sharpness(noisy) > sp.sharpness(flat)


def test_exposure_penalty_flags_crushed_blacks():
    crushed = Image.new("L", (64, 64), 2)
    assert sp.exposure_penalty(crushed) > 1.0


def test_exposure_penalty_flags_blown_highlights():
    blown = Image.new("L", (64, 64), 253)
    assert sp.exposure_penalty(blown) > 1.0


def test_exposure_penalty_flags_flat_low_contrast_frame():
    flat = Image.new("L", (64, 64), 128)
    assert sp.exposure_penalty(flat) > 0.5


def test_exposure_penalty_of_well_exposed_varied_image_is_low():
    noisy = _noisy_gray()
    assert sp.exposure_penalty(noisy) < 0.5


def test_dhash_of_identical_image_matches_itself():
    im = _noisy_gray()
    assert sp.hamming(sp.dhash(im), sp.dhash(im)) == 0


def test_dhash_of_very_different_images_has_large_hamming_distance():
    a = _noisy_gray(seed=1)
    b = Image.new("L", (64, 64), 200)
    assert sp.hamming(sp.dhash(a), sp.dhash(b)) > 10


def test_analyse_handles_a_corrupt_file_without_raising(tmp_path):
    bad = tmp_path / "broken.jpg"
    bad.write_bytes(b"not a real jpeg")
    photo = sp.Photo(path=bad, taken=None)
    sp.analyse([photo])
    assert photo.sharp == 0.0
    assert photo.expo_pen == 2.0


def test_analyse_sets_score_higher_for_sharper_photos(tmp_path):
    sharp_path = make_photo_file(tmp_path, "sharp.jpg", seed=1)
    flat_path = make_photo_file(tmp_path, "flat.jpg", color=(128, 128, 128))
    sharp = sp.Photo(path=sharp_path, taken=None)
    flat = sp.Photo(path=flat_path, taken=None)
    sp.analyse([sharp, flat])
    assert sharp.score > flat.score


# ---------------------------------------------------------------------------- bucketing


def _target(id_, date_str, kind="day", tz=None):
    return sp.Target(id=id_, date=dt.date.fromisoformat(date_str), kind=kind, tz=tz)


def test_bucket_assigns_photo_to_matching_day():
    targets = [_target("d1", "2026-08-01")]
    photo = sp.Photo(path=None, taken=dt.datetime(2026, 8, 1, 10, 0))
    result = sp.bucket([photo], targets)
    assert result["d1"] == [photo]
    assert photo.bucket == "d1"


def test_bucket_photo_with_no_taken_time_is_unmatched(capsys):
    targets = [_target("d1", "2026-08-01")]
    photo = sp.Photo(path=None, taken=None)
    result = sp.bucket([photo], targets)
    assert result["d1"] == []
    assert "1 photo" in capsys.readouterr().out


def test_bucket_photo_on_unlisted_date_is_unmatched():
    targets = [_target("d1", "2026-08-01")]
    photo = sp.Photo(path=None, taken=dt.datetime(2026, 9, 1, 10, 0))
    result = sp.bucket([photo], targets)
    assert result["d1"] == []


def test_bucket_uses_the_days_local_zone_east_of_utc():
    # 23:30 UTC on Aug 1 is 08:30 on Aug 2 in Tokyo -> the Aug 2 day
    targets = [
        _target("d1", "2026-08-01", tz="Asia/Tokyo"),
        _target("d2", "2026-08-02", tz="Asia/Tokyo"),
    ]
    photo = sp.Photo(path=None, taken=dt.datetime(2026, 8, 1, 23, 30))
    result = sp.bucket([photo], targets)
    assert result["d2"] == [photo]
    assert result["d1"] == []


def test_bucket_uses_the_days_local_zone_west_of_utc():
    # 02:00 UTC on Aug 2 is still 22:00 on Aug 1 in New York -> the Aug 1 day
    targets = [
        _target("d1", "2026-08-01", tz="America/New_York"),
        _target("d2", "2026-08-02", tz="America/New_York"),
    ]
    photo = sp.Photo(path=None, taken=dt.datetime(2026, 8, 2, 2, 0))
    result = sp.bucket([photo], targets)
    assert result["d1"] == [photo]


def test_bucket_each_day_uses_its_own_zone_on_a_multi_zone_trip():
    # Lisbon (UTC+1 in August) then the Azores (UTC+0 in August)
    targets = [
        _target("lis", "2026-08-04", tz="Europe/Lisbon"),
        _target("azo", "2026-08-05", tz="Atlantic/Azores"),
    ]
    lisbon_evening = sp.Photo(path=None, taken=dt.datetime(2026, 8, 4, 21, 0))  # 22:00 Lisbon
    azores_morning = sp.Photo(path=None, taken=dt.datetime(2026, 8, 5, 9, 0))  # 09:00 Azores
    result = sp.bucket([lisbon_evening, azores_morning], targets)
    assert result["lis"] == [lisbon_evening]
    assert result["azo"] == [azores_morning]


def test_bucket_photo_in_the_seam_between_zones_goes_to_the_nearest_day():
    # Tokyo's Aug 1 ends at 15:00 UTC; London's Aug 2 starts at 23:00 UTC Aug 1.
    # 16:00 UTC falls in neither window: 1h past Tokyo's day, 7h before London's.
    targets = [
        _target("tok", "2026-08-01", tz="Asia/Tokyo"),
        _target("lon", "2026-08-02", tz="Europe/London"),
    ]
    photo = sp.Photo(path=None, taken=dt.datetime(2026, 8, 1, 16, 0))
    result = sp.bucket([photo], targets)
    assert result["tok"] == [photo]


def test_bucket_local_wall_clock_photo_is_not_shifted_by_the_zone():
    # camera time 23:30 with no offset recorded: already local, stays on Aug 1
    targets = [
        _target("d1", "2026-08-01", tz="Asia/Tokyo"),
        _target("d2", "2026-08-02", tz="Asia/Tokyo"),
    ]
    photo = sp.Photo(path=None, taken=dt.datetime(2026, 8, 1, 23, 30), local=True)
    result = sp.bucket([photo], targets)
    assert result["d1"] == [photo]


def test_day_window_utc_is_local_midnight_to_midnight():
    start, end = sp.day_window_utc(_target("d", "2026-08-05", tz="Europe/Lisbon"))
    assert (start, end) == (dt.datetime(2026, 8, 4, 23, 0), dt.datetime(2026, 8, 5, 23, 0))


# ------------------------------------------------------------------------- dedupe / pick


def _scored_photo(score, dhash_val, path=None):
    p = sp.Photo(path=path, taken=None)
    p.score = score
    p.dhash = dhash_val
    return p


def test_dedupe_drops_near_identical_photos_keeping_the_higher_score():
    best = _scored_photo(0.9, 0)
    worse_dupe = _scored_photo(0.5, 0)  # identical dhash -> distance 0
    kept = sp.dedupe([best, worse_dupe], max_dist=10)
    assert kept == [best]


def test_dedupe_keeps_photos_that_are_visually_distinct():
    a = _scored_photo(0.9, 0)
    b = _scored_photo(0.8, 0xFFFFFFFFFFFFFFFF)  # maximally different dhash
    kept = sp.dedupe([a, b], max_dist=5)
    assert kept == [a, b] or kept == [b, a]


def test_pick_diverse_returns_at_most_n():
    photos = [_scored_photo(1.0 - i * 0.1, i) for i in range(5)]
    picks = sp.pick_diverse(photos, n=2, spread_dist=0)
    assert len(picks) == 2


def test_pick_diverse_with_n_larger_than_pool_returns_everything():
    photos = [_scored_photo(0.9, 0), _scored_photo(0.5, 1)]
    picks = sp.pick_diverse(photos, n=10, spread_dist=50)
    assert len(picks) == 2


def test_pick_diverse_loosens_spread_requirement_when_pool_too_similar():
    # all photos share the same dhash, so no pair meets spread_dist=50 -
    # the "loosen" fallback must still return n picks by score alone.
    photos = [_scored_photo(1.0 - i * 0.1, 0) for i in range(4)]
    picks = sp.pick_diverse(photos, n=3, spread_dist=50)
    assert len(picks) == 3
    assert picks[0].score == pytest.approx(1.0)


def test_pick_diverse_prefers_best_score_first():
    photos = [_scored_photo(0.2, 10), _scored_photo(0.9, 20), _scored_photo(0.5, 30)]
    picks = sp.pick_diverse(photos, n=1, spread_dist=0)
    assert picks[0].score == pytest.approx(0.9)


# --------------------------------------------------------------------------- load_*


def test_load_targets_builds_one_target_per_day():
    spec = {
        "timeline": [
            {"type": "day", "key": "lisbon", "dayIndex": 1, "date": "2026-08-01"},
            {"type": "day", "key": "lisbon", "dayIndex": 2, "date": "2026-08-02"},
            {"type": "transit"},
        ]
    }
    targets = sp.load_targets(spec)
    assert [t.id for t in targets] == ["lisbon-d1", "lisbon-d2"]


def test_load_targets_includes_layover_by_daterange_start():
    spec = {"timeline": [{"type": "layover", "key": "porto", "dateRange": ["2026-08-05", None]}]}
    targets = sp.load_targets(spec)
    assert targets[0].id == "porto"
    assert targets[0].date == dt.date(2026, 8, 5)


def test_load_targets_resolves_each_days_zone_from_its_city():
    spec = {
        "locations": {
            "lis": {"label": "Lisbon", "lat": 38.72, "lon": -9.14},
            "cap": {"label": "Capelas", "lat": 37.83, "lon": -25.69},
        },
        "timeline": [
            {"type": "day", "key": "lisbon", "dayIndex": 1, "date": "2026-08-04", "city": "Lisbon"},
            {
                "type": "day",
                "key": "capelas",
                "dayIndex": 1,
                "date": "2026-08-05",
                "city": "capelas",
            },
        ],
    }
    assert [t.tz for t in sp.load_targets(spec)] == ["Europe/Lisbon", "Atlantic/Azores"]


def test_load_targets_unresolved_place_borrows_the_neighbouring_zone():
    spec = {
        "locations": {"lis": {"label": "Lisbon", "lat": 38.72, "lon": -9.14}},
        "timeline": [
            {"type": "day", "key": "x", "dayIndex": 1, "date": "2026-08-03", "city": "Nowhere"},
            {"type": "day", "key": "lisbon", "dayIndex": 1, "date": "2026-08-04", "city": "Lisbon"},
            {"type": "day", "key": "y", "dayIndex": 1, "date": "2026-08-05", "city": "Unknown"},
        ],
    }
    assert [t.tz for t in sp.load_targets(spec)] == ["Europe/Lisbon"] * 3


def test_load_targets_without_locations_leaves_zone_unset():
    spec = {"timeline": [{"type": "day", "key": "a", "dayIndex": 1, "date": "2026-08-01"}]}
    assert sp.load_targets(spec)[0].tz is None


def test_load_targets_skips_layover_without_daterange(capsys):
    spec = {"timeline": [{"type": "layover", "key": "porto"}]}
    targets = sp.load_targets(spec)
    assert targets == []
    assert "no dateRange" in capsys.readouterr().err


def test_load_from_manifest_skips_missing_files(tmp_path, capsys):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"items": [{"file": "ghost.jpg", "createTime": ""}]}))
    photos = sp.load_from_manifest(manifest, tmp_path)
    assert photos == []
    assert "missing file" in capsys.readouterr().err


def test_load_from_manifest_prefers_orig_dimensions_over_downscaled(tmp_path):
    make_photo_file(tmp_path, "a.jpg", color=(1, 2, 3))
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "items": [
                    {
                        "file": "a.jpg",
                        "createTime": "2026-08-01T10:00:00Z",
                        "width": 100,
                        "height": 100,
                        "orig_width": 4000,
                        "orig_height": 3000,
                    }
                ]
            }
        )
    )
    photos = sp.load_from_manifest(manifest, tmp_path)
    assert (photos[0].width, photos[0].height) == (4000, 3000)


def test_load_from_folder_reads_exif_datetime_as_local_wall_clock(tmp_path):
    make_photo_file(tmp_path, "a.jpg", exif_dt="2026:08:03 10:00:00")
    photos = sp.load_from_folder(tmp_path)
    assert photos[0].taken == dt.datetime(2026, 8, 3, 10, 0, 0)
    assert photos[0].local is True


def test_load_from_folder_with_exif_offset_converts_to_utc(tmp_path):
    make_photo_file(tmp_path, "a.jpg", exif_dt="2026:08:03 10:00:00", exif_offset="+09:00")
    photos = sp.load_from_folder(tmp_path)
    assert photos[0].taken == dt.datetime(2026, 8, 3, 1, 0, 0)
    assert photos[0].local is False


def test_load_from_folder_without_exif_has_no_taken_time(tmp_path):
    # file mtime is deliberately NOT used - a download/copy resets it
    make_photo_file(tmp_path, "a.jpg")
    photos = sp.load_from_folder(tmp_path)
    assert photos[0].taken is None


def test_load_from_folder_ignores_unsupported_extensions(tmp_path):
    (tmp_path / "notes.txt").write_text("hello")
    make_photo_file(tmp_path, "a.jpg")
    photos = sp.load_from_folder(tmp_path)
    assert len(photos) == 1


def test_load_from_folder_skips_unreadable_image(tmp_path, capsys):
    bad = tmp_path / "broken.jpg"
    bad.write_bytes(b"not a real jpeg")
    photos = sp.load_from_folder(tmp_path)
    assert photos == []
    assert "cannot read" in capsys.readouterr().err


# ------------------------------------------------------------------------------- export


def test_export_orders_by_taken_time_by_default(tmp_path):
    p1 = sp.Photo(path=make_photo_file(tmp_path, "a.jpg"), taken=dt.datetime(2026, 8, 2))
    p2 = sp.Photo(path=make_photo_file(tmp_path, "b.jpg"), taken=dt.datetime(2026, 8, 1))
    out_dir = tmp_path / "out"
    rel = sp.export([p1, p2], "day1", out_dir, max_px=64)
    assert rel == ["images/trips/day1-1.jpg", "images/trips/day1-2.jpg"]
    # p2 (earlier) exported first despite being second in the input list
    assert (out_dir / "day1-1.jpg").exists()


def test_export_preserve_order_keeps_input_order(tmp_path):
    p1 = sp.Photo(path=make_photo_file(tmp_path, "a.jpg"), taken=dt.datetime(2026, 8, 2))
    p2 = sp.Photo(path=make_photo_file(tmp_path, "b.jpg"), taken=dt.datetime(2026, 8, 1))
    rel = sp.export([p1, p2], "hero", tmp_path / "out", max_px=64, preserve_order=True)
    assert rel == ["images/trips/hero-1.jpg", "images/trips/hero-2.jpg"]


# --------------------------------------------------------------------------- select()


def _spec_with_days(*day_dates):
    timeline = [
        {
            "type": "day",
            "key": "lisbon",
            "dayIndex": i + 1,
            "dayCount": len(day_dates),
            "date": d,
            "isCheckIn": i == 0,
            "city": "Lisboa",
            "title": "Lisboa",
        }
        for i, d in enumerate(day_dates)
    ]
    return {"meta": {}, "timeline": timeline, "photos": {}}


def test_select_raises_when_spec_has_no_targets():
    with pytest.raises(ValueError):
        sp.select([], {"timeline": []}, None)


def test_select_offline_fills_trip_photos_for_each_day(tmp_path, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    spec = _spec_with_days("2026-08-01")
    photos = [
        sp.Photo(
            path=make_photo_file(tmp_path, f"p{i}.jpg", seed=i),
            taken=dt.datetime(2026, 8, 1, 9 + i),
        )
        for i in range(4)
    ]
    result = sp.select(
        photos,
        spec,
        tmp_path / "images" / "trips",
        per_region=2,
        minimum=1,
        use_ai=False,
        log=lambda m: None,
    )
    day = result["timeline"][0]
    assert len(day["tripPhotos"]) == 2
    for rel in day["tripPhotos"]:
        assert (tmp_path / rel).exists()


def test_select_day_with_no_photos_is_skipped_without_crashing(tmp_path, capsys):
    spec = _spec_with_days("2026-08-01")
    result = sp.select([], spec, tmp_path / "images", use_ai=False, log=print)
    assert "tripPhotos" not in result["timeline"][0]
    assert "no photos that day" in capsys.readouterr().out


def test_select_never_fills_lodging_photos_from_the_album(tmp_path):
    spec = _spec_with_days("2026-08-01")
    photos = [
        sp.Photo(
            path=make_photo_file(tmp_path, f"p{i}.jpg", seed=i),
            taken=dt.datetime(2026, 8, 1, 9 + i),
        )
        for i in range(6)
    ]
    result = sp.select(
        photos,
        spec,
        tmp_path / "images" / "trips",
        per_region=2,
        minimum=1,
        use_ai=False,
        log=lambda m: None,
    )
    assert not result.get("photos", {}).get("lisbon", {}).get("lodging")
    assert not (tmp_path / "images" / "lodging").exists()


def test_select_hero_photos_come_from_the_whole_trip(tmp_path):
    spec = _spec_with_days("2026-08-01", "2026-08-02")
    photos = [
        sp.Photo(
            path=make_photo_file(tmp_path, f"p{i}.jpg", seed=i),
            taken=dt.datetime(2026, 8, 1 + i // 3, 9 + i % 3),
        )
        for i in range(6)
    ]
    result = sp.select(
        photos,
        spec,
        tmp_path / "images" / "trips",
        per_region=2,
        minimum=1,
        use_ai=False,
        log=lambda m: None,
    )
    assert len(result["hero"]["photos"]) == 2


def test_select_dry_run_writes_no_files(tmp_path):
    spec = _spec_with_days("2026-08-01")
    photos = [
        sp.Photo(path=make_photo_file(tmp_path, "p.jpg"), taken=dt.datetime(2026, 8, 1, 9)),
    ]
    images_dir = tmp_path / "images" / "trips"
    sp.select(
        photos,
        spec,
        images_dir,
        use_ai=False,
        dry_run=True,
        minimum=1,
        log=lambda m: None,
    )
    assert not images_dir.exists()


def test_select_never_calls_gemini_when_use_ai_is_false(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-present")

    def _boom(*a, **k):
        raise AssertionError("pick_with_gemini must not run when use_ai=False")

    monkeypatch.setattr(sp, "pick_with_gemini", _boom)
    monkeypatch.setattr(sp, "pick_hero_with_gemini", _boom)
    spec = _spec_with_days("2026-08-01")
    photos = [
        sp.Photo(
            path=make_photo_file(tmp_path, f"p{i}.jpg", seed=i),
            taken=dt.datetime(2026, 8, 1, 9 + i),
        )
        for i in range(3)
    ]
    sp.select(
        photos,
        spec,
        tmp_path / "images" / "trips",
        use_ai=False,
        minimum=1,
        log=lambda m: None,
    )


def test_select_falls_back_to_offline_when_gemini_pick_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-present")
    monkeypatch.setattr(sp, "pick_with_gemini", lambda *a, **k: None)
    monkeypatch.setattr(sp, "pick_hero_with_gemini", lambda *a, **k: None)
    spec = _spec_with_days("2026-08-01")
    photos = [
        sp.Photo(
            path=make_photo_file(tmp_path, f"p{i}.jpg", seed=i),
            taken=dt.datetime(2026, 8, 1, 9 + i),
        )
        for i in range(4)
    ]
    result = sp.select(
        photos,
        spec,
        tmp_path / "images" / "trips",
        per_region=2,
        minimum=1,
        use_ai=True,
        log=lambda m: None,
    )
    assert len(result["timeline"][0]["tripPhotos"]) == 2


# ------------------------------------------------------- small analysis copy + page copy


def _write_manifest(tmp_path, items, downscale_px):
    m = tmp_path / "manifest.json"
    m.write_text(json.dumps({"downscale_px": downscale_px, "items": items}), encoding="utf-8")
    return m


def test_load_from_manifest_page_size_download_is_its_own_page_copy(tmp_path):
    make_photo_file(tmp_path, "a.jpg")
    m = _write_manifest(tmp_path, [{"file": "a.jpg", "createTime": ""}], 1600)
    assert sp.load_from_manifest(m, tmp_path)[0].full_path == tmp_path / "a.jpg"


def test_load_from_manifest_small_download_uses_its_fetched_full_file(tmp_path):
    make_photo_file(tmp_path, "a.jpg")
    (tmp_path / "full").mkdir()
    make_photo_file(tmp_path / "full", "a.jpg")
    make_photo_file(tmp_path, "b.jpg")
    items = [
        {"file": "a.jpg", "createTime": "", "full_file": "full/a.jpg"},
        {"file": "b.jpg", "createTime": ""},
    ]
    photos = sp.load_from_manifest(_write_manifest(tmp_path, items, 512), tmp_path)
    assert photos[0].full_path == tmp_path / "full" / "a.jpg"
    assert photos[1].full_path is None


def _small_and_full_photos(tmp_path, n, day=1):
    """n photos whose analysis copy is 64px and whose page copy (in full/) is 200px."""
    (tmp_path / "full").mkdir(exist_ok=True)
    photos = []
    for i in range(n):
        small = make_photo_file(tmp_path, f"p{i}.jpg", seed=i, size=64)
        make_photo_file(tmp_path / "full", f"p{i}.jpg", seed=i, size=200)
        photos.append(sp.Photo(path=small, taken=dt.datetime(2026, 8, day, 9 + i)))
    return photos


def _never(need):
    raise AssertionError(f"fetch_full must not run, got {need}")


def test_select_fetches_page_copies_for_the_whole_shortlist_once(tmp_path):
    spec = _spec_with_days("2026-08-01")
    photos = _small_and_full_photos(tmp_path, 6)
    calls = []

    def fetch_full(need):
        calls.append(sorted(p.path.name for p in need))
        for p in need:
            p.full_path = tmp_path / "full" / p.path.name

    result = sp.select(
        photos,
        spec,
        tmp_path / "images" / "trips",
        per_region=2,
        minimum=1,
        candidates=4,
        use_ai=False,
        fetch_full=fetch_full,
        log=lambda m: None,
    )
    assert len(calls) == 1
    # day shortlist (4) + hero pool (max(4, 12) -> all 6) -> every photo, once
    assert calls[0] == sorted(f"p{i}.jpg" for i in range(6))
    exported = tmp_path / result["timeline"][0]["tripPhotos"][0]
    with Image.open(exported) as im:
        assert max(im.size) == 200  # from the page copy, not the 64px analysis copy


def test_select_rerun_reuses_existing_page_copies_without_fetching(tmp_path):
    spec = _spec_with_days("2026-08-01")
    photos = _small_and_full_photos(tmp_path, 4)
    for p in photos:
        p.full_path = tmp_path / "full" / p.path.name
    result = sp.select(
        photos,
        spec,
        tmp_path / "images" / "trips",
        per_region=2,
        minimum=1,
        use_ai=False,
        fetch_full=_never,
        log=lambda m: None,
    )
    with Image.open(tmp_path / result["timeline"][0]["tripPhotos"][0]) as im:
        assert max(im.size) == 200


def test_select_without_page_copies_says_so_and_still_exports(tmp_path):
    spec = _spec_with_days("2026-08-01")
    photos = _small_and_full_photos(tmp_path, 3)
    logs = []
    result = sp.select(
        photos,
        spec,
        tmp_path / "images" / "trips",
        per_region=2,
        minimum=1,
        use_ai=False,
        log=logs.append,
    )
    assert any("no page-size copy" in m for m in logs)
    assert len(result["timeline"][0]["tripPhotos"]) == 2


def test_select_dry_run_never_fetches_page_copies(tmp_path):
    spec = _spec_with_days("2026-08-01")
    photos = _small_and_full_photos(tmp_path, 3)
    sp.select(
        photos,
        spec,
        tmp_path / "images" / "trips",
        minimum=1,
        use_ai=False,
        dry_run=True,
        fetch_full=_never,
        log=lambda m: None,
    )
