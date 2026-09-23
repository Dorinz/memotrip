"""Unit tests for build_trip.py's template renderer.

Uses a small self-contained template fixture (not the real, 600-line
trip_template.html) so these tests keep exercising the same placeholder
tokens build() actually fills without breaking every time the real page's
design changes.
"""

from __future__ import annotations

import json

import pytest

import build_trip
import palette

TEMPLATE = """<!doctype html>
<html>
<head><title>{{TITLE}}</title>
<style>
  --bg0:{{C_BG0}}; --bg1:{{C_BG1}}; --bg3:{{C_BG3}}; --ink:{{C_INK}};
  --ink-dim:{{C_INK_DIM}}; --muted:{{C_MUTED}}; --turq:{{C_TURQ}};
  --turq-deep:{{C_TURQ_DEEP}}; --sea:{{C_SEA}}; --sea-deep:{{C_SEA_DEEP}};
  --ink-rgb:{{C_INK_RGB}}; --turq-rgb:{{C_TURQ_RGB}}; --sea-rgb:{{C_SEA_RGB}};
  --dark-bg:{{C_DARK_BG}}; --dark-fg:{{C_DARK_FG}}; --dark-muted:{{C_DARK_MUTED}};
  --dark-fg-rgb:{{C_DARK_FG_RGB}}; --sidebar-bg:{{C_SIDEBAR_BG}};
  --sidebar-ink:{{C_SIDEBAR_INK}}; --sidebar-ink-rgb:{{C_SIDEBAR_INK_RGB}};
</style>
</head>
<body>
  <section class="hero">
    <p class="eyebrow">{{HERO_EYEBROW}}</p>
    <h1>{{HERO_H1}}</h1>
    <p class="sub">{{HERO_SUB}}</p>
    <p class="region">{{HERO_REGION}}</p>
    <div class="meta">{{HERO_META}}</div>
    <script>const heroPhotos = {{HERO_PHOTOS_JSON}};</script>
  </section>

  <!-- ================= ECONOMICS (bonus appendix) ================= -->
  <section class="econ">
    <p class="eyebrow">{{ECON_EYEBROW}}</p>
    <h2>{{ECON_H2}}</h2>
    <p class="intro">{{ECON_INTRO}}</p>
    <div class="big">{{ECON_BIG}}</div>
    <div class="bars">{{ECON_BARS}}</div>
    <div class="notes">{{ECON_NOTES}}</div>
    <p class="foot">{{ECON_FOOT}}</p>
  </section>

  <section class="outro">
    <p class="eyebrow">{{OUTRO_EYEBROW}}</p>
    <h2>{{OUTRO_H2}}</h2>
    <p>{{OUTRO_P}}</p>
    <div class="stats">{{OUTRO_STATS}}</div>
  </section>

  <script>
    const locations = {{LOCATIONS_JSON}};
    const photos = {{PHOTOS_JSON}};
    const timeline = {{TIMELINE_JSON}};
  </script>
</body>
</html>
"""


def _minimal_spec() -> dict:
    return {
        "meta": {"title": "Test Trip"},
        "hero": {"eyebrow": "", "h1": "", "sub": "", "region": "", "meta": [], "photos": []},
        "outro": {"eyebrow": "", "h2": "", "p": "", "stats": []},
        "locations": {},
        "photos": {},
        "timeline": [],
    }


def test_build_with_minimal_spec_does_not_raise():
    html = build_trip.build(_minimal_spec(), TEMPLATE)
    assert "<html>" in html
    assert "Test Trip" in html


def test_build_leaves_no_unfilled_placeholders():
    html = build_trip.build(_minimal_spec(), TEMPLATE)
    assert "{{" not in html and "}}" not in html


def test_build_raises_if_template_has_an_unknown_placeholder():
    bad_template = TEMPLATE.replace("{{TITLE}}", "{{TITLE}} {{NOT_A_REAL_KEY}}")
    with pytest.raises(SystemExit):
        build_trip.build(_minimal_spec(), bad_template)


def test_economics_section_is_removed_when_spec_has_no_economics_data():
    html = build_trip.build(_minimal_spec(), TEMPLATE)
    assert "ECONOMICS" not in html
    assert 'class="econ"' not in html
    # sections either side of the removed block must survive intact
    assert 'class="hero"' in html
    assert 'class="outro"' in html


def test_economics_section_is_kept_when_spec_has_economics_data():
    spec = _minimal_spec()
    spec["economics"] = {"intro": "we spent a lot", "bars": [], "big": [], "notes": [], "foot": ""}
    html = build_trip.build(spec, TEMPLATE)
    assert 'class="econ"' in html
    assert "we spent a lot" in html


def test_missing_theme_falls_back_to_default_palette():
    spec = _minimal_spec()
    assert "theme" not in spec
    html = build_trip.build(spec, TEMPLATE)
    assert palette.DEFAULT["ink"] in html
    assert palette.DEFAULT["turquoise"] in html


def test_custom_theme_colors_are_used_verbatim():
    spec = _minimal_spec()
    spec["theme"] = palette.generate_palette("fixed-seed")
    assert spec["theme"]["ink"] != palette.DEFAULT["ink"]  # sanity: seed actually changed it
    html = build_trip.build(spec, TEMPLATE)
    assert spec["theme"]["ink"] in html
    assert palette.DEFAULT["ink"] not in html


def test_timeline_json_round_trips_through_the_page():
    spec = _minimal_spec()
    spec["timeline"] = [{"type": "day", "key": "lisbon", "date": "2026-08-03"}]
    html = build_trip.build(spec, TEMPLATE)
    start = html.index("const timeline = ") + len("const timeline = ")
    end = html.index(";", start)
    assert json.loads(html[start:end]) == spec["timeline"]


def test_special_characters_in_json_do_not_break_the_script_tag():
    spec = _minimal_spec()
    spec["locations"] = {"x": {"label": 'Café </script> "quoted" עברית', "lat": 1.0, "lon": 2.0}}
    html = build_trip.build(spec, TEMPLATE)
    # the raw string must still be valid, parseable JSON embedded in the page
    start = html.index("const locations = ") + len("const locations = ")
    end = html.index(";", start)
    parsed = json.loads(html[start:end])
    assert parsed["x"]["label"] == 'Café </script> "quoted" עברית'


def test_hero_meta_stats_render_as_divs():
    spec = _minimal_spec()
    spec["hero"]["meta"] = [{"n": "9", "l": "nights"}, {"n": "3", "l": "islands"}]
    html = build_trip.build(spec, TEMPLATE)
    assert '<div class="n">9</div><div class="l">nights</div>' in html
    assert '<div class="n">3</div><div class="l">islands</div>' in html


def test_build_is_idempotent_for_the_same_spec():
    spec = _minimal_spec()
    assert build_trip.build(spec, TEMPLATE) == build_trip.build(spec, TEMPLATE)
