"""Unit tests for parse_docs.py: date parsing, the regex logistics parser,
AI/regex assembly, and splitting multi-night stays into per-day items.

parse_ai(), geocode() and _gemini_coords() are the only network/AI-touching
functions here; every test for them mocks the boundary (requests.get or
gemini_util) so the suite never makes a real HTTP call.
"""

from __future__ import annotations

import json
import time

import parse_docs

# --------------------------------------------------------------------- heb_date


def test_heb_date_with_hebrew_month_and_year():
    assert parse_docs.heb_date("3 באוגוסט 2026") == "2026-08-03"


def test_heb_date_with_hebrew_month_no_year_uses_default_year():
    assert parse_docs.heb_date("3 באוגוסט", default_year=2027) == "2027-08-03"


def test_heb_date_with_slash_format():
    assert parse_docs.heb_date("12/8") is not None
    assert parse_docs.heb_date("12/8/2026") == "2026-08-12"


def test_heb_date_with_dot_format_and_two_digit_year():
    assert parse_docs.heb_date("12.8.26") == "2026-08-12"


def test_heb_date_with_no_recognizable_date_returns_none():
    assert parse_docs.heb_date("just some random text") is None


def test_heb_date_default_year_falls_back_to_current_year(monkeypatch):
    monkeypatch.setattr(time, "localtime", lambda: time.struct_time((2030, 1, 1, 0, 0, 0, 0, 1, 0)))
    assert parse_docs.heb_date("5/9") == "2030-09-05"


# ------------------------------------------------------------------- parse_docx_regex


def test_regex_parses_a_single_flight():
    text = "3 באוגוסט 2026\nטיסה TP8920: (TLV) -> (LIS) 17:10 -> 21:10"
    rx = parse_docs.parse_docx_regex(text)
    assert len(rx["flights"]) == 1
    f = rx["flights"][0]
    assert f["flightNo"] == "TP8920"
    assert f["from"] == "תל אביב"  # IATA TLV mapped via the IATA table
    assert f["to"] == "Lisboa"
    assert f["dep"] == "17:10"
    assert f["arr"] == "21:10"


def test_regex_flight_with_unknown_airport_code_keeps_the_code():
    text = "טיסה XX123: (ZZZ) -> (LIS) 10:00 -> 12:00"
    rx = parse_docs.parse_docx_regex(text)
    assert rx["flights"][0]["from"] == "ZZZ"  # not in IATA table -> passed through raw


def test_regex_ignores_a_flight_line_with_no_airport_codes():
    text = "טיסה TP123: משהו בלי קודים"
    rx = parse_docs.parse_docx_regex(text)
    f = rx["flights"][0]
    assert f["from"] is None and f["to"] is None


def test_regex_parses_a_ferry_with_two_ports():
    text = (
        "10 באוגוסט 2026: מפיקו (Cais do Pico) לסאו ז'ורז' (Velas) | "
        "יציאה ב-08:30, הגעה ב-09:20 | הזמנה 68898"
    )
    rx = parse_docs.parse_docx_regex(text)
    assert len(rx["ferries"]) == 1
    fe = rx["ferries"][0]
    assert fe["from"] == "Cais do Pico"
    assert fe["to"] == "Velas"
    assert fe["dep"] == "08:30"
    assert fe["arr"] == "09:20"
    assert fe["booking"] == "68898"


def test_regex_skips_a_pipe_line_with_only_one_port():
    text = "10 באוגוסט 2026: הגעה (Velas) | יציאה ב-08:30, הגעה ב-09:20"
    rx = parse_docs.parse_docx_regex(text)
    assert rx["ferries"] == []


def test_regex_parses_a_lodging_stay_with_host_and_code():
    text = (
        "הטיול שלנו ב-2026\n" "3 באוגוסט – 4 באוגוסט | ליסבון: Nice Flat (אצל Maria) | קוד: ABC123"
    )
    rx = parse_docs.parse_docx_regex(text)
    assert len(rx["stays"]) == 1
    s = rx["stays"][0]
    assert s["place"] == "ליסבון"
    assert s["name"] == "Nice Flat"
    assert s["host"] == "Maria"
    assert s["code"] == "ABC123"
    assert s["dateRange"] == ["2026-08-03", "2026-08-04"]


def test_regex_stay_without_host_or_code():
    text = "3 באוגוסט – 4 באוגוסט | ליסבון: Nice Flat"
    rx = parse_docs.parse_docx_regex(text)
    s = rx["stays"][0]
    assert s["host"] is None
    assert s["code"] is None


def test_regex_extracts_year_from_first_matching_line():
    text = "משהו לפני\nהטיול שלנו ב-2026\nטיסה TP1: (TLV) -> (LIS) 10:00 -> 12:00"
    rx = parse_docs.parse_docx_regex(text)
    assert rx["year"] == 2026


def test_regex_with_no_year_anywhere_in_text():
    text = "טיסה TP1: (TLV) -> (LIS) 10:00 -> 12:00"
    rx = parse_docs.parse_docx_regex(text)
    assert rx["year"] is None


def test_regex_on_empty_text_returns_empty_lists():
    rx = parse_docs.parse_docx_regex("")
    assert rx == {"year": None, "flights": [], "ferries": [], "stays": []}


# -------------------------------------------------------------------------- slug


def test_slug_basic():
    taken = set()
    assert parse_docs.slug("Lisboa", taken) == "lisboa"


def test_slug_strips_non_alphanumeric():
    taken = set()
    assert parse_docs.slug("São Jorge!", taken) == "sojorge"


def test_slug_empty_name_falls_back_to_loc():
    taken = set()
    assert parse_docs.slug("", taken) == "loc"


def test_slug_deduplicates_against_taken_set():
    taken = set()
    first = parse_docs.slug("Lisboa", taken)
    second = parse_docs.slug("Lisboa", taken)
    third = parse_docs.slug("Lisboa", taken)
    assert (first, second, third) == ("lisboa", "lisboa2", "lisboa3")


# -------------------------------------------------------------------------- assemble


def test_assemble_with_no_ai_falls_back_to_regex_events():
    rx = {
        "year": 2026,
        "flights": [
            {
                "flightNo": "TP1",
                "from": "Lisboa",
                "to": "Ponta Delgada",
                "date": "2026-08-03",
                "dep": "10:00",
                "arr": "12:00",
                "raw": "",
            }
        ],
        "ferries": [],
        "stays": [],
    }
    spec = parse_docs.assemble(None, rx)
    assert len(spec["timeline"]) == 1
    assert spec["timeline"][0]["type"] == "transit"
    assert spec["meta"]["title"] == "טיול 2026"


def test_assemble_with_empty_ai_timeline_falls_back_to_regex():
    rx = {"year": None, "flights": [], "ferries": [], "stays": []}
    spec = parse_docs.assemble({"timeline": [], "places": []}, rx)
    assert spec["timeline"] == []


def test_assemble_with_empty_timeline_but_nonempty_places_still_falls_back_to_regex():
    # an empty `timeline` list is falsy, so `ai and ai.get("timeline")` is False
    # even though `places` is non-empty - assemble() treats this exactly like
    # ai=None (regex fallback), and never registers the AI-only places either.
    ai = {
        "meta": {"title": "Trip"},
        "timeline": [],
        "places": [{"name": "Unused Town", "lat": 1.0, "lon": 2.0}],
    }
    rx = {"year": None, "flights": [], "ferries": [], "stays": []}
    spec = parse_docs.assemble(ai, rx)
    assert spec["locations"] == {}


def test_assemble_registers_every_ai_place_even_if_unreferenced():
    ai = {
        "meta": {"title": "Trip"},
        "timeline": [{"type": "stay", "city": "Lisboa", "dateRange": ["2026-08-01", "2026-08-02"]}],
        "places": [{"name": "Unused Town", "lat": 1.0, "lon": 2.0}],
    }
    rx = {"year": None, "flights": [], "ferries": [], "stays": []}
    spec = parse_docs.assemble(ai, rx)
    assert any(v["label"] == "Unused Town" for v in spec["locations"].values())


def test_assemble_stay_without_key_gets_one_from_city():
    ai = {
        "meta": {},
        "timeline": [{"type": "stay", "city": "Lisboa", "dateRange": ["2026-08-01", "2026-08-02"]}],
        "places": [],
    }
    rx = {"year": None, "flights": [], "ferries": [], "stays": []}
    spec = parse_docs.assemble(ai, rx)
    assert spec["timeline"][0]["key"] == "lisboa"
    assert "lisboa" in spec["photos"]


def test_assemble_drama_infers_from_and_to_from_neighbors():
    ai = {
        "meta": {},
        "timeline": [
            {"type": "transit", "mode": "plane", "from": "Lisboa", "to": "Porto"},
            {"type": "drama"},
            {"type": "stay", "city": "Faro", "dateRange": ["2026-08-05", "2026-08-06"]},
        ],
        "places": [],
    }
    rx = {"year": None, "flights": [], "ferries": [], "stays": []}
    spec = parse_docs.assemble(ai, rx)
    drama = spec["timeline"][1]
    stay = spec["timeline"][2]
    porto_key = spec["timeline"][0]["to"]
    # the drama's `to` resolves through the *location* registry (keyed by the
    # raw city name), which for a stay's own city agrees with the stay's own
    # `key` field (see test_stay_key_and_its_own_location_key_agree below).
    faro_loc_key = next(k for k, v in spec["locations"].items() if v["label"] == "Faro")
    assert faro_loc_key == stay["key"]
    assert drama["from"] == porto_key
    assert drama["to"] == faro_loc_key
    assert drama["fog"] == faro_loc_key


def test_stay_key_and_its_own_location_key_agree():
    # regression test: slug() used to dedupe a stay's own item key and its
    # city's location key against one shared `taken` set, so the very first
    # mention of a city always diverged (item key "faro", location key
    # "faro2") even though the two live in unrelated dicts (spec["photos"]
    # vs spec["locations"]) and never needed to avoid each other.
    ai = {
        "meta": {},
        "timeline": [{"type": "stay", "city": "Faro", "dateRange": ["2026-08-05", "2026-08-06"]}],
        "places": [],
    }
    rx = {"year": None, "flights": [], "ferries": [], "stays": []}
    spec = parse_docs.assemble(ai, rx)
    stay = spec["timeline"][0]
    loc_key = next(k for k, v in spec["locations"].items() if v["label"] == "Faro")
    assert stay["key"] == "faro"
    assert loc_key == "faro"


def test_two_stays_in_the_same_city_get_distinct_item_keys_but_share_one_location():
    ai = {
        "meta": {},
        "timeline": [
            {"type": "stay", "city": "Lisboa", "dateRange": ["2026-08-01", "2026-08-02"]},
            {"type": "transit", "mode": "plane", "from": "Lisboa", "to": "Porto"},
            {"type": "stay", "city": "Lisboa", "dateRange": ["2026-08-10", "2026-08-11"]},
        ],
        "places": [],
    }
    rx = {"year": None, "flights": [], "ferries": [], "stays": []}
    spec = parse_docs.assemble(ai, rx)
    first_stay, second_stay = spec["timeline"][0], spec["timeline"][2]
    assert first_stay["key"] != second_stay["key"]
    lisboa_locations = [v for v in spec["locations"].values() if v["label"] == "Lisboa"]
    assert len(lisboa_locations) == 1  # exactly one map entry for the shared city


def test_assemble_drama_fog_defaults_from_to_when_missing():
    ai = {
        "meta": {},
        "timeline": [{"type": "drama", "from": "Lisboa", "to": "Porto"}],
        "places": [],
    }
    rx = {"year": None, "flights": [], "ferries": [], "stays": []}
    spec = parse_docs.assemble(ai, rx)
    drama = spec["timeline"][0]
    assert drama["fog"] == drama["to"]


def test_assemble_uses_ai_coords_over_geocoded_coords():
    ai = {
        "meta": {},
        "timeline": [{"type": "stay", "city": "Lisboa", "dateRange": ["2026-08-01", "2026-08-02"]}],
        "places": [{"name": "Lisboa", "lat": 1.1, "lon": 2.2}],
    }
    rx = {"year": None, "flights": [], "ferries": [], "stays": []}
    geo = {"Lisboa": {"lat": 9.9, "lon": 9.9}}
    spec = parse_docs.assemble(ai, rx, geo)
    loc = next(iter(spec["locations"].values()))
    assert (loc["lat"], loc["lon"]) == (1.1, 2.2)


def test_regex_fallback_stay_key_and_its_own_location_key_agree():
    # same regression as test_stay_key_and_its_own_location_key_agree, for
    # the regex-fallback branch of assemble() (used when there's no AI data).
    rx = {
        "year": None,
        "flights": [],
        "ferries": [],
        "stays": [
            {"place": "Faro", "name": "Nice Flat", "dateRange": ["2026-08-05", "2026-08-06"]}
        ],
    }
    spec = parse_docs.assemble(None, rx)
    stay = spec["timeline"][0]
    loc_key = next(k for k, v in spec["locations"].items() if v["label"] == "Faro")
    assert stay["key"] == loc_key == "faro"


def test_assemble_regex_events_are_sorted_chronologically():
    rx = {
        "year": 2026,
        "flights": [
            {
                "flightNo": "TP2",
                "from": "B",
                "to": "C",
                "date": "2026-08-05",
                "dep": None,
                "arr": None,
                "raw": "",
            },
            {
                "flightNo": "TP1",
                "from": "A",
                "to": "B",
                "date": "2026-08-01",
                "dep": None,
                "arr": None,
                "raw": "",
            },
        ],
        "ferries": [],
        "stays": [],
    }
    spec = parse_docs.assemble(None, rx)
    assert [it["flightNo"] for it in spec["timeline"]] == ["TP1", "TP2"]


# ------------------------------------------------------------------- expand_days


def test_stay_without_daterange_is_left_unsplit():
    stay = {"type": "stay", "key": "x", "city": "Lisboa"}
    assert parse_docs._stay_to_days(stay) == [stay]


def test_stay_to_days_splits_into_one_item_per_night():
    stay = {"type": "stay", "key": "lisboa", "dateRange": ["2026-08-01", "2026-08-04"], "city": "L"}
    days = parse_docs._stay_to_days(stay)
    assert len(days) == 3
    assert [d["date"] for d in days] == ["2026-08-01", "2026-08-02", "2026-08-03"]
    assert days[0]["isCheckIn"] is True
    assert days[1]["isCheckIn"] is False
    assert days[2]["isCheckIn"] is False
    assert all(d["dayCount"] == 3 for d in days)


def test_stay_to_days_same_checkin_checkout_gives_one_night():
    stay = {"type": "stay", "key": "x", "dateRange": ["2026-08-01", "2026-08-01"], "city": "L"}
    days = parse_docs._stay_to_days(stay)
    assert len(days) == 1
    assert days[0]["isCheckIn"] is True


def test_stay_to_days_copies_checkin_only_fields_to_first_day_alone():
    stay = {
        "type": "stay",
        "key": "x",
        "dateRange": ["2026-08-01", "2026-08-03"],
        "city": "L",
        "host": "Maria",
        "code": "ABC",
    }
    days = parse_docs._stay_to_days(stay)
    assert days[0]["host"] == "Maria" and days[0]["code"] == "ABC"
    assert "host" not in days[1] and "code" not in days[1]


def test_expand_days_only_splits_stays_and_keeps_order():
    spec = {
        "timeline": [
            {"type": "transit", "mode": "plane"},
            {"type": "stay", "key": "x", "dateRange": ["2026-08-01", "2026-08-03"], "city": "L"},
            {"type": "layover", "key": "y"},
        ]
    }
    out = parse_docs.expand_days(spec)["timeline"]
    assert out[0]["type"] == "transit"
    assert out[1]["type"] == "day"
    assert out[2]["type"] == "day"
    assert out[3]["type"] == "layover"


# -------------------------------------------------------------------------- fill_coords


def test_fill_coords_skips_network_call_when_everything_already_has_coords(tmp_path, monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("geocode() should not be called when nothing is missing")

    monkeypatch.setattr(parse_docs, "geocode", _boom)
    spec = {"locations": {"a": {"label": "Lisboa", "lat": 1.0, "lon": 2.0}}}
    unresolved = parse_docs.fill_coords(spec, "", "model", tmp_path / "cache.json")
    assert unresolved == []


def test_fill_coords_falls_back_to_gemini_for_unresolved_names(tmp_path, monkeypatch):
    monkeypatch.setattr(parse_docs, "geocode", lambda names, cache, hint: {"Lisboa": None})
    monkeypatch.setattr(
        parse_docs, "_gemini_coords", lambda names, model, log: {"Lisboa": {"lat": 3.0, "lon": 4.0}}
    )
    spec = {"locations": {"a": {"label": "Lisboa", "lat": None, "lon": None}}}
    unresolved = parse_docs.fill_coords(spec, "", "model", tmp_path / "cache.json")
    assert unresolved == []
    assert spec["locations"]["a"]["lat"] == 3.0


def test_fill_coords_reports_names_still_unresolved(tmp_path, monkeypatch):
    monkeypatch.setattr(parse_docs, "geocode", lambda names, cache, hint: {})
    monkeypatch.setattr(parse_docs, "_gemini_coords", lambda names, model, log: {})
    spec = {"locations": {"a": {"label": "Nowhereville", "lat": None, "lon": None}}}
    unresolved = parse_docs.fill_coords(spec, "", "model", tmp_path / "cache.json")
    assert unresolved == ["Nowhereville"]


# ---------------------------------------------------------------------------- geocode


def test_geocode_caches_a_hit(tmp_path, monkeypatch):
    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return [{"lat": "37.7", "lon": "-25.6"}]

    monkeypatch.setattr(parse_docs.time, "sleep", lambda s: None)
    monkeypatch.setattr("requests.get", lambda *a, **k: _Resp())
    cache_path = tmp_path / "geo.json"
    result = parse_docs.geocode(["Ponta Delgada"], cache_path)
    assert result["Ponta Delgada"] == {"lat": 37.7, "lon": -25.6}
    assert cache_path.exists()


def test_geocode_records_none_for_no_hits(tmp_path, monkeypatch):
    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return []

    monkeypatch.setattr(parse_docs.time, "sleep", lambda s: None)
    monkeypatch.setattr("requests.get", lambda *a, **k: _Resp())
    result = parse_docs.geocode(["Nowhere"], tmp_path / "geo.json")
    assert result["Nowhere"] is None


def test_geocode_network_failure_is_caught_and_recorded_as_none(tmp_path, monkeypatch):
    def _boom(*a, **k):
        raise ConnectionError("network down")

    monkeypatch.setattr(parse_docs.time, "sleep", lambda s: None)
    monkeypatch.setattr("requests.get", _boom)
    result = parse_docs.geocode(["Somewhere"], tmp_path / "geo.json")
    assert result["Somewhere"] is None


def test_geocode_skips_names_already_cached(tmp_path, monkeypatch):
    cache_path = tmp_path / "geo.json"
    cache_path.write_text('{"Lisboa": {"lat": 1.0, "lon": 2.0}}', encoding="utf-8")

    def _boom(*a, **k):
        raise AssertionError("should not re-fetch an already-cached hit")

    monkeypatch.setattr("requests.get", _boom)
    result = parse_docs.geocode(["Lisboa"], cache_path)
    assert result["Lisboa"] == {"lat": 1.0, "lon": 2.0}


# ------------------------------------------------------------------ consistency


def _stay(city, a, b):
    return {"type": "stay", "city": city, "dateRange": [a, b]}


def _transit(mode, d):
    return {"type": "transit", "mode": mode, "date": d, "from": "x", "to": "y"}


def test_consistency_clean_timeline_has_no_warnings():
    spec = {
        "timeline": [
            _stay("Ponta Delgada", "2026-08-06", "2026-08-09"),
            _transit("plane", "2026-08-09"),
            _stay("Pico", "2026-08-09", "2026-08-10"),
            _transit("ferry", "2026-08-10"),
            _stay("Velas", "2026-08-10", "2026-08-12"),
        ]
    }
    assert parse_docs.check_consistency(spec) == []
    assert "_warnings" not in spec


def test_consistency_flags_a_night_with_no_lodging():
    spec = {
        "timeline": [
            _stay("Velas", "2026-08-09", "2026-08-11"),
            _stay("Faial", "2026-08-12", "2026-08-13"),
        ]
    }
    (w,) = parse_docs.check_consistency(spec)
    assert "no lodging for 1 night(s) 2026-08-11 -> 2026-08-12" in w


def test_consistency_flags_overlapping_stays():
    spec = {
        "timeline": [_stay("A", "2026-08-01", "2026-08-04"), _stay("B", "2026-08-03", "2026-08-05")]
    }
    (w,) = parse_docs.check_consistency(spec)
    assert w.startswith("stays overlap: A until 2026-08-04, B from 2026-08-03")


def test_consistency_flags_a_transit_inside_a_stay():
    # the DOCX version of the Azores trip: Velas from the 9th, but the ferry there is on the 10th
    spec = {
        "timeline": [
            _stay("Capelas", "2026-08-06", "2026-08-09"),
            _stay("Velas", "2026-08-09", "2026-08-11"),
            _transit("ferry", "2026-08-10"),
        ]
    }
    (w,) = parse_docs.check_consistency(spec)
    assert "ferry on 2026-08-10 falls in the middle of the Velas stay" in w


def test_consistency_transit_on_checkin_or_checkout_day_is_fine():
    spec = {
        "timeline": [
            _transit("plane", "2026-08-01"),
            _stay("A", "2026-08-01", "2026-08-03"),
            _transit("plane", "2026-08-03"),
        ]
    }
    assert parse_docs.check_consistency(spec) == []


def test_consistency_appends_to_existing_warnings_and_flags_bad_dates():
    spec = {
        "_warnings": ["earlier"],
        "timeline": [
            _stay("A", "2026-08-01", "2026-08-02"),
            {"type": "stay", "city": "broken", "dateRange": ["?", None]},
            _stay("B", "2026-08-03", "2026-08-04"),
        ],
    }
    parse_docs.check_consistency(spec)
    assert spec["_warnings"] == [
        "earlier",
        "broken stay has no valid check-in/check-out dates",
        "no lodging for 1 night(s) 2026-08-02 -> 2026-08-03 (between A and B)",
    ]


def test_consistency_flags_stays_with_no_dates():
    spec = {"timeline": [{"type": "stay", "city": "Velas"}]}
    (w,) = parse_docs.check_consistency(spec)
    assert w == "Velas stay has no valid check-in/check-out dates"


def test_assemble_carries_model_reported_conflicts_into_warnings():
    ai = {
        "timeline": [_stay("Pico", "2026-08-09", "2026-08-10")],
        "places": [],
        "conflicts": ["PDF has Pico on 9 Aug, DOCX has Velas; kept the PDF"],
    }
    spec = parse_docs.assemble(ai, {"flights": [], "ferries": [], "stays": []})
    assert spec["_warnings"] == [
        "sources disagree: PDF has Pico on 9 Aug, DOCX has Velas; kept the PDF"
    ]


# ------------------------------------------------- cut-short answers + retries

_RX_15 = {"flights": [{}] * 4, "ferries": [{}] * 3, "stays": [{}] * 8}


def _ai(n_stays):
    stay = {"type": "stay", "city": "X", "dateRange": ["2026-08-01", "2026-08-02"]}
    return {"timeline": [stay] * n_stays, "places": []}


def test_cut_short_flags_an_empty_timeline():
    assert parse_docs._cut_short({"timeline": []}, None) == "empty timeline"


def test_cut_short_flags_far_fewer_items_than_the_regex_found():
    assert "only 2 flights/ferries/stays, the regex parser found 15" in parse_docs._cut_short(
        _ai(2), _RX_15
    )


def test_cut_short_accepts_a_full_answer():
    assert parse_docs._cut_short(_ai(15), _RX_15) is None


def test_cut_short_skips_the_comparison_when_the_regex_found_little():
    rx = {"flights": [{}], "ferries": [], "stays": [{}]}
    assert parse_docs._cut_short(_ai(1), rx) is None


def _scripted_gemini(monkeypatch, answers):
    """Make gemini_util.client() return a fake whose generate_content answers
    with the given (payload, finish_reason) pairs in turn."""
    import json
    from types import SimpleNamespace

    import gemini_util

    calls = []

    def generate_content(**kw):
        calls.append(kw)
        payload, finish = answers.pop(0)
        text = payload if isinstance(payload, str) else json.dumps(payload)
        return SimpleNamespace(
            text=text,
            candidates=[SimpleNamespace(finish_reason=SimpleNamespace(name=finish))],
            usage_metadata=SimpleNamespace(candidates_token_count=1, thoughts_token_count=1),
        )

    monkeypatch.setattr(
        gemini_util, "client", lambda *a: SimpleNamespace(
            models=SimpleNamespace(generate_content=generate_content)
        )
    )
    return calls


def test_parse_ai_retries_an_answer_closed_early_and_returns_the_full_one(monkeypatch):
    calls = _scripted_gemini(monkeypatch, [(_ai(2), "STOP"), (_ai(15), "STOP")])
    got = parse_docs.parse_ai("text", "m", log=lambda m: None, rx=_RX_15)
    assert len(got["timeline"]) == 15
    assert len(calls) == 2
    assert calls[0]["config"].max_output_tokens == parse_docs.AI_MAX_OUTPUT_TOKENS


def test_parse_ai_retries_a_loop_cut_off_at_the_token_limit(monkeypatch):
    stuck = '{"timeline": [{"type": "stay", "note": "key: lisbon1. key: lisbon1. key: lis'
    calls = _scripted_gemini(monkeypatch, [(stuck, "MAX_TOKENS"), (_ai(15), "STOP")])
    assert len(parse_docs.parse_ai("text", "m", log=lambda m: None, rx=_RX_15)["timeline"]) == 15
    assert len(calls) == 2


def test_parse_ai_gives_up_after_three_bad_answers_and_returns_none(monkeypatch):
    calls = _scripted_gemini(monkeypatch, [("{", "MAX_TOKENS")] * 3)
    messages = []
    assert parse_docs.parse_ai("text", "m", log=messages.append, rx=_RX_15) is None
    assert len(calls) == 3
    assert any("AI extraction failed" in m for m in messages)


def test_bloated_accepts_normal_field_lengths():
    ai = {"timeline": [{"type": "stay", "host": "Sara"}], "conflicts": ["x" * 249]}
    assert parse_docs._bloated(ai) is None


def test_bloated_flags_a_runaway_text_field_with_its_path():
    ai = {"timeline": [{"type": "stay"}, {"type": "transit", "note": "key: lisbon1. " * 300}]}
    found = parse_docs._bloated(ai)
    assert found.startswith(".timeline[1].note is 4200 chars long")


def test_parse_ai_retries_an_answer_with_a_runaway_field(monkeypatch):
    bloated = _ai(15)
    bloated["timeline"] = bloated["timeline"] + [{"type": "transit", "note": "✈️" * 1000}]
    calls = _scripted_gemini(monkeypatch, [(bloated, "STOP"), (_ai(15), "STOP")])
    got = parse_docs.parse_ai("text", "m", log=lambda m: None, rx=_RX_15)
    assert len(got["timeline"]) == 15
    assert len(calls) == 2


def test_extraction_schema_has_no_free_text_note_anywhere():
    assert "note" not in json.dumps(parse_docs.AI_SCHEMA)
    assert "`note`" not in parse_docs.AI_PROMPT


def test_schema_requires_city_and_dates_on_stays_and_route_on_transits():
    transit, stay, drama = parse_docs.AI_SCHEMA["properties"]["timeline"]["items"]["anyOf"]
    assert set(transit["required"]) == {"type", "mode", "from", "to", "date"}
    assert set(stay["required"]) == {"type", "city", "dateRange"}
    assert stay["properties"]["type"]["enum"] == ["stay", "layover"]
    assert drama["required"] == ["type"]


def test_undated_stay_flags_a_stay_without_dates():
    ai = {"timeline": [{"type": "stay", "city": "Lisbon"}]}
    assert parse_docs._undated_stay(ai) == "Lisbon stay has no check-in/check-out dates"


def test_undated_stay_accepts_dated_stays_and_ignores_transits():
    ai = {
        "timeline": [
            {"type": "transit", "date": "2026-08-03"},
            {"type": "stay", "city": "Lisbon", "dateRange": ["2026-08-03", "2026-08-04"]},
        ]
    }
    assert parse_docs._undated_stay(ai) is None


def test_parse_ai_retries_an_answer_with_undated_stays(monkeypatch):
    undated = {"timeline": [{"type": "stay", "city": "X"}] * 15, "places": []}
    dated = {
        "timeline": [{"type": "stay", "city": "X", "dateRange": ["2026-08-01", "2026-08-02"]}] * 15,
        "places": [],
    }
    calls = _scripted_gemini(monkeypatch, [(undated, "STOP"), (dated, "STOP")])
    got = parse_docs.parse_ai("text", "m", log=lambda m: None, rx=_RX_15)
    assert got["timeline"][0]["dateRange"] == ["2026-08-01", "2026-08-02"]
    assert len(calls) == 2
