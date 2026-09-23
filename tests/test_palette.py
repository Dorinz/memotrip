"""Unit tests for palette.py's color-theme generator.

Each trip gets exactly one palette, persisted in trip_spec.json, so the two
properties that matter most are determinism (same seed -> same theme, so a
rebuild never shifts colors) and the contrast guarantees the module's own
docstring promises (readable text on every generated background).
"""

from __future__ import annotations

import palette

REQUIRED_KEYS = {
    "bg0",
    "bg1",
    "bg2",
    "bg3",
    "ink",
    "ink_dim",
    "muted",
    "turquoise",
    "turquoise_deep",
    "sea",
    "sea_deep",
    "dark_bg",
    "dark_fg",
    "dark_muted",
    "sidebar_bg",
    "sidebar_ink",
    "ink_rgb",
    "turquoise_rgb",
    "sea_rgb",
    "dark_fg_rgb",
    "sidebar_ink_rgb",
}


def test_same_seed_is_deterministic():
    a = palette.generate_palette("azores-2026")
    b = palette.generate_palette("azores-2026")
    assert a == b


def test_different_seeds_usually_differ():
    a = palette.generate_palette("seed-one")
    b = palette.generate_palette("seed-two")
    assert a != b


def test_palette_has_every_required_key():
    p = palette.generate_palette("seed")
    assert REQUIRED_KEYS <= p.keys()


def test_all_hex_colors_are_well_formed():
    p = palette.generate_palette("seed")
    hex_keys = REQUIRED_KEYS - {
        "ink_rgb",
        "turquoise_rgb",
        "sea_rgb",
        "dark_fg_rgb",
        "sidebar_ink_rgb",
    }
    for key in hex_keys:
        value = p[key]
        assert value.startswith("#") and len(value) == 7
        int(value[1:], 16)  # raises ValueError if not valid hex


def test_rgb_strings_match_their_hex_counterpart():
    p = palette.generate_palette("seed")
    assert p["ink_rgb"] == "%d,%d,%d" % palette._rgb(p["ink"])
    assert p["turquoise_rgb"] == "%d,%d,%d" % palette._rgb(p["turquoise"])
    assert p["sea_rgb"] == "%d,%d,%d" % palette._rgb(p["sea"])
    assert p["dark_fg_rgb"] == "%d,%d,%d" % palette._rgb(p["dark_fg"])
    assert p["sidebar_ink_rgb"] == "%d,%d,%d" % palette._rgb(p["sidebar_ink"])


def test_default_palette_has_every_required_key():
    assert REQUIRED_KEYS <= palette.DEFAULT.keys()


def test_contrast_is_symmetric():
    a, b = "#163B3F", "#FBF9F4"
    assert palette._contrast(a, b) == palette._contrast(b, a)


def test_contrast_of_a_color_with_itself_is_one():
    assert palette._contrast("#123456", "#123456") == 1.0


def test_contrast_ratios_meet_the_documented_minimums_across_many_seeds():
    # generate_palette() actively darkens/lightens colors until these ratios
    # hold (see _darken_until); a regression here would silently ship
    # low-contrast, hard-to-read pages.
    for seed in range(200):
        p = palette.generate_palette(seed)
        assert palette._contrast(p["ink"], p["bg0"]) >= 9.0, seed
        assert palette._contrast(p["dark_fg"], p["dark_bg"]) >= 8.0, seed
        assert palette._contrast(p["sidebar_ink"], p["sidebar_bg"]) >= 4.5, seed
        assert palette._contrast(p["turquoise_deep"], p["bg0"]) >= 4.5, seed
        assert palette._contrast(p["sea_deep"], p["bg0"]) >= 4.5, seed


def test_no_seed_falls_back_to_a_random_but_still_valid_palette():
    p = palette.generate_palette(None)
    assert REQUIRED_KEYS <= p.keys()
