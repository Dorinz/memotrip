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


def _save_jpeg(path, im, exif_dt: str | None = None):
    kwargs = {}
    if exif_dt is not None:
        exif = Image.Exif()
        exif[sp.EXIF_DATETIME_ORIGINAL] = exif_dt
        kwargs["exif"] = exif.tobytes()
    im.save(path, "JPEG", **kwargs)


def _noisy_gray(size=64, seed=0):
    rnd = random.Random(seed)
    im = Image.new("L", (size, size))
    im.putdata([rnd.randint(0, 255) for _ in range(size * size)])
    return im


def make_photo_file(tmp_path, name, *, seed=0, color=None, exif_dt=None, size=64):
    """Writes a real JPEG to disk: a noisy (sharp) grayscale image by default,
    or a flat color image when `color` is given (for blur/exposure tests)."""
    im = Image.new("RGB", (size, size), color) if color else _noisy_gray(size, seed).convert("RGB")
    path = tmp_path / name
    _save_jpeg(path, im, exif_dt)
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


def _target(id_, date_str, kind="day"):
    return sp.Target(id=id_, date=dt.date.fromisoformat(date_str), kind=kind)


def test_bucket_assigns_photo_to_matching_day():
    targets = [_target("d1", "2026-08-01")]
    photo = sp.Photo(path=None, taken=dt.datetime(2026, 8, 1, 10, 0))
    result = sp.bucket([photo], targets, tz_offset_h=0)
    assert result["d1"] == [photo]
    assert photo.bucket == "d1"


def test_bucket_photo_with_no_taken_time_is_unmatched(capsys):
    targets = [_target("d1", "2026-08-01")]
    photo = sp.Photo(path=None, taken=None)
    result = sp.bucket([photo], targets, tz_offset_h=0)
    assert result["d1"] == []
    assert "1 photo" in capsys.readouterr().out


def test_bucket_photo_on_unlisted_date_is_unmatched():
    targets = [_target("d1", "2026-08-01")]
    photo = sp.Photo(path=None, taken=dt.datetime(2026, 9, 1, 10, 0))
    result = sp.bucket([photo], targets, tz_offset_h=0)
    assert result["d1"] == []


def test_bucket_timezone_offset_shifts_a_photo_across_midnight():
    # taken at 23:30 UTC on the 1st; with +2h offset local time becomes 01:30
    # on the 2nd, so it belongs to the 2nd's target, not the 1st's.
    targets = [_target("d1", "2026-08-01"), _target("d2", "2026-08-02")]
    photo = sp.Photo(path=None, taken=dt.datetime(2026, 8, 1, 23, 30))
    result = sp.bucket([photo], targets, tz_offset_h=2)
    assert result["d2"] == [photo]
    assert result["d1"] == []


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


def test_load_from_folder_reads_exif_datetime(tmp_path):
    make_photo_file(tmp_path, "a.jpg", exif_dt="2026:08:03 10:00:00")
    photos = sp.load_from_folder(tmp_path)
    assert photos[0].taken == dt.datetime(2026, 8, 3, 10, 0, 0)


def test_load_from_folder_falls_back_to_mtime_without_exif(tmp_path):
    make_photo_file(tmp_path, "a.jpg")
    photos = sp.load_from_folder(tmp_path)
    assert photos[0].taken is not None


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
    return {"meta": {"tz_offset_hours": 0}, "timeline": timeline, "photos": {}}


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
        lodging_count=0,
        log=lambda m: None,
    )
    day = result["timeline"][0]
    assert len(day["tripPhotos"]) == 2
    for rel in day["tripPhotos"]:
        assert (tmp_path / rel).exists()


def test_select_day_with_no_photos_is_skipped_without_crashing(tmp_path, capsys):
    spec = _spec_with_days("2026-08-01")
    result = sp.select([], spec, tmp_path / "images", use_ai=False, lodging_count=0, log=print)
    assert "tripPhotos" not in result["timeline"][0]
    assert "no photos that day" in capsys.readouterr().out


def test_select_checkin_day_also_fills_lodging_photos(tmp_path):
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
        lodging_count=2,
        log=lambda m: None,
    )
    assert len(result["photos"]["lisbon"]["lodging"]) == 2


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
        lodging_count=0,
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
        lodging_count=0,
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
        lodging_count=0,
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
        lodging_count=0,
        log=lambda m: None,
    )
    assert len(result["timeline"][0]["tripPhotos"]) == 2
