"""Unit tests for gen_copy.py: the itinerary digest, the fill()/merge logic,
and generate_copy()'s error handling.

gemini_util.client()/generate_json() are always mocked here - these tests
verify gen_copy's own control flow (what it sends, how it merges the
response back into the spec, what happens when a call fails), not Gemini.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import gen_copy

# ------------------------------------------------------------------------- digest


def test_digest_formats_transit_item():
    spec = {
        "timeline": [
            {"type": "transit", "mode": "plane", "date": "2026-08-03", "from": "a", "to": "b"}
        ]
    }
    out = gen_copy.digest(spec)
    assert "TRANSIT plane 2026-08-03: a -> b" in out


def test_digest_formats_drama_item():
    spec = {"timeline": [{"type": "drama", "from": "a", "to": "b"}]}
    out = gen_copy.digest(spec)
    assert "DRAMA (flight disruption): a <-> b" in out


def test_digest_formats_day_item_with_checkin_flag():
    spec = {
        "timeline": [
            {
                "type": "day",
                "key": "lisbon",
                "dayIndex": 1,
                "dayCount": 3,
                "city": "Lisboa",
                "island": "",
                "date": "2026-08-01",
                "isCheckIn": True,
                "host": "Maria",
            }
        ]
    }
    out = gen_copy.digest(spec)
    assert "DAY key=lisbon day=1/3" in out
    assert "CHECK-IN" in out
    assert "host=Maria" in out


def test_digest_formats_layover_item():
    spec = {
        "timeline": [
            {"type": "layover", "key": "x", "city": "Porto", "island": "", "dateRange": ["a", "b"]}
        ]
    }
    out = gen_copy.digest(spec)
    assert "LAYOVER key=x" in out


def test_digest_skips_unknown_item_types_without_raising():
    spec = {"timeline": [{"type": "something_new"}]}
    assert gen_copy.digest(spec) == ""


def test_digest_on_empty_timeline_is_empty_string():
    assert gen_copy.digest({"timeline": []}) == ""


# ---------------------------------------------------------------------------- fill


def test_fill_copies_present_values():
    dst, src = {}, {"title": "Hello"}
    changed = gen_copy.fill(dst, src, ("title",), overwrite=False)
    assert dst == {"title": "Hello"}
    assert changed == ["title"]


def test_fill_skips_empty_values_in_source():
    dst, src = {"title": "keep"}, {"title": "", "lede": None, "tags": []}
    changed = gen_copy.fill(dst, src, ("title", "lede", "tags"), overwrite=False)
    assert dst == {"title": "keep"}
    assert changed == []


def test_fill_without_overwrite_does_not_replace_existing_value():
    dst, src = {"title": "original"}, {"title": "new"}
    changed = gen_copy.fill(dst, src, ("title",), overwrite=False)
    assert dst["title"] == "original"
    assert changed == []


def test_fill_with_overwrite_replaces_existing_value():
    dst, src = {"title": "original"}, {"title": "new"}
    changed = gen_copy.fill(dst, src, ("title",), overwrite=True)
    assert dst["title"] == "new"
    assert changed == ["title"]


def test_fill_ignores_keys_not_in_the_keys_list():
    dst, src = {}, {"title": "a", "secret": "b"}
    gen_copy.fill(dst, src, ("title",), overwrite=False)
    assert "secret" not in dst


# ---------------------------------------------------------------------- generate_copy


def _spec_with_one_day():
    return {
        "meta": {},
        "hero": {},
        "outro": {},
        "timeline": [
            {
                "type": "day",
                "key": "lisbon",
                "dayIndex": 1,
                "dayCount": 1,
                "city": "Lisboa",
                "island": "Portugal",
                "date": "2026-08-01",
                "isCheckIn": True,
            }
        ],
    }


@pytest.fixture()
def fake_gemini_util(monkeypatch):
    """Replaces gemini_util.client()/generate_json() with a scripted stub the
    test configures per-call, without gen_copy ever touching the real API."""
    calls = []

    def _install(trip_response=None, days_response=None, trip_exc=None, days_exc=None):
        import gemini_util

        def fake_generate_json(model, prompt, *, schema, temperature, cl=None, log=print):
            calls.append({"prompt": prompt, "schema": schema})
            is_days_call = "days" in schema.get("properties", {})
            if is_days_call:
                if days_exc:
                    raise days_exc
                return days_response
            if trip_exc:
                raise trip_exc
            return trip_response

        monkeypatch.setattr(gemini_util, "client", lambda: SimpleNamespace())
        monkeypatch.setattr(gemini_util, "generate_json", fake_generate_json)

    return SimpleNamespace(install=_install, calls=calls)


def test_generate_copy_fills_hero_and_outro_and_day_prose(fake_gemini_util):
    spec = _spec_with_one_day()
    fake_gemini_util.install(
        trip_response={
            "meta": {"title": "כותרת"},
            "hero": {"eyebrow": "e", "h1": "h", "sub": "s", "region": "r", "meta": []},
            "outro": {"eyebrow": "e2", "h2": "h2", "p": "p", "stats": []},
        },
        days_response={
            "days": [
                {
                    "key": "lisbon",
                    "dayIndex": 1,
                    "title": "יום ראשון",
                    "island": "Portugal",
                    "lede": "עשינו כל מיני דברים",
                    "tags": ["a", "b"],
                }
            ]
        },
    )
    result = gen_copy.generate_copy(spec, "תיאור הטיול", model="m", log=lambda m: None)
    assert result["meta"]["title"] == "כותרת"
    assert result["hero"]["h1"] == "h"
    assert result["outro"]["p"] == "p"
    day = result["timeline"][0]
    assert day["title"] == "יום ראשון"
    assert day["lede"] == "עשינו כל מיני דברים"
    assert "_warnings" not in result


def test_generate_copy_skips_drama_line_when_no_drama_in_timeline(fake_gemini_util):
    spec = _spec_with_one_day()
    fake_gemini_util.install(
        trip_response={
            "meta": {},
            "hero": {"eyebrow": "", "h1": "", "sub": "", "region": "", "meta": []},
            "outro": {"eyebrow": "", "h2": "", "p": "", "stats": []},
        },
        days_response={"days": []},
    )
    gen_copy.generate_copy(spec, "desc", model="m", log=lambda m: None)
    trip_call = next(c for c in fake_gemini_util.calls if "days" not in c["schema"]["properties"])
    assert "flight disruption" not in trip_call["prompt"]


def test_generate_copy_includes_drama_line_when_timeline_has_drama(fake_gemini_util):
    spec = _spec_with_one_day()
    spec["timeline"].append({"type": "drama"})
    fake_gemini_util.install(
        trip_response={
            "meta": {},
            "hero": {"eyebrow": "", "h1": "", "sub": "", "region": "", "meta": []},
            "outro": {"eyebrow": "", "h2": "", "p": "", "stats": []},
            "drama": {"eyebrow": "e", "legs": [], "paras": [], "badge": "b"},
        },
        days_response={"days": []},
    )
    result = gen_copy.generate_copy(spec, "desc", model="m", log=lambda m: None)
    trip_call = next(c for c in fake_gemini_util.calls if "days" not in c["schema"]["properties"])
    assert "flight disruption" in trip_call["prompt"]
    drama_item = next(it for it in result["timeline"] if it["type"] == "drama")
    assert drama_item["eyebrow"] == "e"


def test_generate_copy_trip_level_failure_does_not_block_day_copy(fake_gemini_util):
    spec = _spec_with_one_day()
    fake_gemini_util.install(
        trip_exc=RuntimeError("trip call failed"),
        days_response={
            "days": [
                {
                    "key": "lisbon",
                    "dayIndex": 1,
                    "title": "כותרת יום",
                    "lede": "תיאור",
                    "tags": [],
                }
            ]
        },
    )
    result = gen_copy.generate_copy(spec, "desc", model="m", log=lambda m: None)
    assert result["timeline"][0]["title"] == "כותרת יום"
    assert any("trip-level copy failed" in w for w in result["_warnings"])


def test_generate_copy_day_level_failure_is_recorded_as_a_warning(fake_gemini_util):
    spec = _spec_with_one_day()
    fake_gemini_util.install(
        trip_response={
            "meta": {},
            "hero": {"eyebrow": "", "h1": "", "sub": "", "region": "", "meta": []},
            "outro": {"eyebrow": "", "h2": "", "p": "", "stats": []},
        },
        days_exc=RuntimeError("days call failed"),
    )
    result = gen_copy.generate_copy(spec, "desc", model="m", log=lambda m: None)
    assert any("day copy failed" in w for w in result["_warnings"])
    assert "title" not in result["timeline"][0]  # never got filled


def test_generate_copy_unmapped_day_response_is_ignored(fake_gemini_util):
    spec = _spec_with_one_day()
    fake_gemini_util.install(
        trip_response={
            "meta": {},
            "hero": {"eyebrow": "", "h1": "", "sub": "", "region": "", "meta": []},
            "outro": {"eyebrow": "", "h2": "", "p": "", "stats": []},
        },
        days_response={
            "days": [
                {"key": "no-such-day", "dayIndex": 99, "title": "orphan", "lede": "x", "tags": []}
            ]
        },
    )
    result = gen_copy.generate_copy(spec, "desc", model="m", log=lambda m: None)
    assert "title" not in result["timeline"][0]
