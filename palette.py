"""Generate a harmonious color theme for a trip page, from three deliberately
distinct hue families rather than one hue at different shades:
  - a warm cream/parchment "paper" background — always warm, so the page
    reads as an inviting travel journal regardless of the accent color;
  - a vivid "accent" hue (turquoise, links, the sidebar block, map marks) —
    kept in a cool-to-vivid range so it always pops against the warm paper;
  - a distinct "ink" hue, offset far enough from the accent to read as a
    genuinely different color (not a darker shade of the accent) — text,
    the "sea" secondary accent, and the dark immersive day-sections all
    draw from it, e.g. turquoise accent + navy ink, not turquoise-on-turquoise.
Pure color theory - no external service, no network call.

Each trip gets ONE palette, generated once and stored in trip_spec.json's
`theme` block, so rebuilds (e.g. after adding photos) don't shift the colors.
"""

from __future__ import annotations

import colorsys
import random


def _hex(h: float, s: float, lightness: float) -> str:
    h = (h % 360) / 360.0
    r, g, b = colorsys.hls_to_rgb(h, max(0.0, min(1.0, lightness)), max(0.0, min(1.0, s)))
    return "#{:02X}{:02X}{:02X}".format(round(r * 255), round(g * 255), round(b * 255))


def _rgb(hexcolor: str) -> tuple[int, int, int]:
    hexcolor = hexcolor.lstrip("#")
    return tuple(int(hexcolor[i : i + 2], 16) for i in (0, 2, 4))


def _luminance(hexcolor: str) -> float:
    def lin(c):
        c /= 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = _rgb(hexcolor)
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def _contrast(a: str, b: str) -> float:
    la, lb = _luminance(a), _luminance(b)
    la, lb = max(la, lb), min(la, lb)
    return (la + 0.05) / (lb + 0.05)


def _darken_until(h: float, s: float, lightness: float, bg: str, min_ratio: float) -> str:
    """Step lightness down until the color contrasts >= min_ratio against bg (or bottoms out)."""
    hexcolor = _hex(h, s, lightness)
    steps = 0
    while _contrast(hexcolor, bg) < min_ratio and lightness > 0.06 and steps < 40:
        lightness -= 0.02
        hexcolor = _hex(h, s, lightness)
        steps += 1
    return hexcolor


def generate_palette(seed=None) -> dict:
    rnd = random.Random(seed)

    # paper is always a warm cream/parchment — the one hue that does NOT rotate,
    # so every trip reads as the same kind of warm, inviting journal
    bg_hue = rnd.uniform(28, 48)
    # the vivid accent (turquoise, links, map marks, the sidebar block) rotates
    # across a wide cool-to-vivid arc, but stays out of the cream's own range so
    # it always pops against the paper instead of blending into it
    accent_hue = rnd.uniform(140, 320)
    # the "ink" hue — text, the secondary "sea" accent, and the dark day-sections
    # — is offset FAR from the accent (70-140°, either direction) so it reads as
    # a genuinely different color, e.g. turquoise accent + navy ink, not two
    # shades of the same hue
    ink_offset = rnd.uniform(70, 140) * rnd.choice((1, -1))
    ink_hue = (accent_hue + ink_offset) % 360

    bg0 = _hex(bg_hue, rnd.uniform(0.20, 0.34), rnd.uniform(0.965, 0.98))
    bg1 = _hex(bg_hue, rnd.uniform(0.16, 0.26), rnd.uniform(0.945, 0.965))
    bg3 = _hex(bg_hue, rnd.uniform(0.14, 0.24), rnd.uniform(0.91, 0.94))
    bg2 = "#FFFFFF"

    ink = _darken_until(ink_hue, rnd.uniform(0.45, 0.65), 0.20, bg0, 9.0)
    ink_dim = _hex(ink_hue, rnd.uniform(0.18, 0.28), rnd.uniform(0.35, 0.42))
    muted = _hex(ink_hue, rnd.uniform(0.14, 0.22), rnd.uniform(0.38, 0.45))

    turquoise = _hex(accent_hue, rnd.uniform(0.55, 0.68), rnd.uniform(0.40, 0.46))
    turquoise_deep = _darken_until(accent_hue, rnd.uniform(0.60, 0.72), 0.32, bg0, 4.5)
    sea = _hex(ink_hue, rnd.uniform(0.42, 0.56), rnd.uniform(0.40, 0.48))
    sea_deep = _darken_until(ink_hue, rnd.uniform(0.48, 0.60), 0.32, bg0, 4.5)

    # a dark/light pair on the ink hue, for a section that inverts to a deep
    # immersive background instead of the page's usual light paper tone (e.g. a
    # template's "day" sections vs. lighter "in transit" sections)
    dark_bg = _hex(ink_hue, rnd.uniform(0.32, 0.44), rnd.uniform(0.16, 0.20))
    dark_fg = _darken_until(ink_hue, rnd.uniform(0.25, 0.40), 0.94, dark_bg, 8.0)
    # _darken_until only steps lightness down; dark_fg needs to stay light against a
    # dark bg, so contrast-check by lightening instead if the initial pick falls short
    if _contrast(dark_fg, dark_bg) < 8.0:
        lightness = 0.94
        while _contrast(dark_fg, dark_bg) < 8.0 and lightness < 0.99:
            lightness += 0.01
            dark_fg = _hex(ink_hue, rnd.uniform(0.25, 0.40), lightness)
    dark_muted = _hex(ink_hue, rnd.uniform(0.18, 0.28), rnd.uniform(0.62, 0.70))

    # a bold, saturated block (not a subtle paper tint) for a high-contrast,
    # playful panel — e.g. a persistent sidebar. Background on the vivid accent
    # hue, text on the distinct ink hue, so the panel itself carries the same
    # two-hue contrast as the rest of the page instead of being monochrome.
    sidebar_bg = _hex(accent_hue, rnd.uniform(0.55, 0.72), rnd.uniform(0.82, 0.88))
    sidebar_ink = _darken_until(ink_hue, rnd.uniform(0.70, 0.92), 0.30, sidebar_bg, 4.5)

    return {
        "bg0": bg0,
        "bg1": bg1,
        "bg2": bg2,
        "bg3": bg3,
        "ink": ink,
        "ink_dim": ink_dim,
        "muted": muted,
        "turquoise": turquoise,
        "turquoise_deep": turquoise_deep,
        "sea": sea,
        "sea_deep": sea_deep,
        "dark_bg": dark_bg,
        "dark_fg": dark_fg,
        "dark_muted": dark_muted,
        "sidebar_bg": sidebar_bg,
        "sidebar_ink": sidebar_ink,
        "ink_rgb": "%d,%d,%d" % _rgb(ink),
        "turquoise_rgb": "%d,%d,%d" % _rgb(turquoise),
        "sea_rgb": "%d,%d,%d" % _rgb(sea),
        "dark_fg_rgb": "%d,%d,%d" % _rgb(dark_fg),
        "sidebar_ink_rgb": "%d,%d,%d" % _rgb(sidebar_ink),
    }


# the Azores reference page's original hand-tuned palette — used whenever a spec
# has no `theme` block, so that build stays byte-identical to the hand-built page
DEFAULT = {
    "bg0": "#FBF9F4",
    "bg1": "#F0F7F6",
    "bg2": "#FFFFFF",
    "bg3": "#E3F1EF",
    "ink": "#163B3F",
    "ink_dim": "#4C6A6D",
    "muted": "#52696B",
    "turquoise": "#1EACA0",
    "turquoise_deep": "#0E8478",
    "sea": "#3C7DA6",
    "sea_deep": "#275A7D",
    "dark_bg": "#132A38",
    "dark_fg": "#DCEFF0",
    "dark_muted": "#7FAEB8",
    "sidebar_bg": "#C1F0EF",
    "sidebar_ink": "#0F578A",
    "ink_rgb": "22,59,63",
    "turquoise_rgb": "30,172,160",
    "sea_rgb": "60,125,166",
    "dark_fg_rgb": "220,239,240",
    "sidebar_ink_rgb": "15,87,138",
}


if __name__ == "__main__":
    import sys

    p = generate_palette(sys.argv[1] if len(sys.argv) > 1 else None)
    for k, v in p.items():
        print(f"{k:16} {v}")
    print(f"\ncontrast ink/bg0: {_contrast(p['ink'], p['bg0']):.1f}:1")
    print(f"contrast turquoise_deep/bg0: {_contrast(p['turquoise_deep'], p['bg0']):.1f}:1")
    print(f"contrast sea_deep/bg0: {_contrast(p['sea_deep'], p['bg0']):.1f}:1")
