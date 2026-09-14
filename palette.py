"""Generate a harmonious color theme for a trip page: a warm near-white "paper"
background family + dark ink text derived from one hue, plus two accent hues in
an analogous relationship (like the hand-tuned turquoise/sea pair on the Azores
page). Pure color theory - no external service, no network call.

Each trip gets ONE palette, generated once and stored in trip_spec.json's
`theme` block, so rebuilds (e.g. after adding photos) don't shift the colors.
"""
from __future__ import annotations

import colorsys
import random


def _hex(h: float, s: float, l: float) -> str:
    h = (h % 360) / 360.0
    r, g, b = colorsys.hls_to_rgb(h, max(0.0, min(1.0, l)), max(0.0, min(1.0, s)))
    return "#{:02X}{:02X}{:02X}".format(round(r * 255), round(g * 255), round(b * 255))


def _rgb(hexcolor: str) -> tuple[int, int, int]:
    hexcolor = hexcolor.lstrip("#")
    return tuple(int(hexcolor[i:i + 2], 16) for i in (0, 2, 4))


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


def _darken_until(h: float, s: float, l: float, bg: str, min_ratio: float) -> str:
    """Step lightness down until the color contrasts >= min_ratio against bg (or bottoms out)."""
    hexcolor = _hex(h, s, l)
    steps = 0
    while _contrast(hexcolor, bg) < min_ratio and l > 0.06 and steps < 40:
        l -= 0.02
        hexcolor = _hex(h, s, l)
        steps += 1
    return hexcolor


def generate_palette(seed=None) -> dict:
    rnd = random.Random(seed)

    accent_hue = rnd.uniform(0, 360)                      # the "brand" hue (was ~178, turquoise)
    offset = rnd.uniform(24, 46) * rnd.choice((1, -1))
    accent2_hue = (accent_hue + offset) % 360              # analogous partner (was ~205, sea)
    bg_hue = (accent_hue + 180 + rnd.uniform(-18, 18)) % 360  # roughly complementary "paper" tone

    bg0 = _hex(bg_hue, rnd.uniform(0.18, 0.32), rnd.uniform(0.965, 0.98))
    bg1 = _hex(accent_hue, rnd.uniform(0.28, 0.40), rnd.uniform(0.945, 0.965))
    bg3 = _hex(accent_hue, rnd.uniform(0.32, 0.45), rnd.uniform(0.91, 0.94))
    bg2 = "#FFFFFF"

    ink = _darken_until(accent_hue, rnd.uniform(0.38, 0.55), 0.20, bg0, 9.0)
    ink_dim = _hex(accent_hue, rnd.uniform(0.14, 0.22), rnd.uniform(0.37, 0.43))
    muted = _hex(accent_hue, rnd.uniform(0.10, 0.18), rnd.uniform(0.40, 0.45))

    turquoise = _hex(accent_hue, rnd.uniform(0.55, 0.68), rnd.uniform(0.40, 0.46))
    turquoise_deep = _darken_until(accent_hue, rnd.uniform(0.60, 0.72), 0.32, bg0, 4.5)
    sea = _hex(accent2_hue, rnd.uniform(0.40, 0.55), rnd.uniform(0.42, 0.50))
    sea_deep = _darken_until(accent2_hue, rnd.uniform(0.45, 0.58), 0.34, bg0, 4.5)

    return {
        "bg0": bg0, "bg1": bg1, "bg2": bg2, "bg3": bg3,
        "ink": ink, "ink_dim": ink_dim, "muted": muted,
        "turquoise": turquoise, "turquoise_deep": turquoise_deep,
        "sea": sea, "sea_deep": sea_deep,
        "ink_rgb": "%d,%d,%d" % _rgb(ink),
        "turquoise_rgb": "%d,%d,%d" % _rgb(turquoise),
        "sea_rgb": "%d,%d,%d" % _rgb(sea),
    }


# the Azores reference page's original hand-tuned palette — used whenever a spec
# has no `theme` block, so that build stays byte-identical to the hand-built page
DEFAULT = {
    "bg0": "#FBF9F4", "bg1": "#F0F7F6", "bg2": "#FFFFFF", "bg3": "#E3F1EF",
    "ink": "#163B3F", "ink_dim": "#4C6A6D", "muted": "#52696B",
    "turquoise": "#1EACA0", "turquoise_deep": "#0E8478",
    "sea": "#3C7DA6", "sea_deep": "#275A7D",
    "ink_rgb": "22,59,63", "turquoise_rgb": "30,172,160", "sea_rgb": "60,125,166",
}


if __name__ == "__main__":
    import sys
    p = generate_palette(sys.argv[1] if len(sys.argv) > 1 else None)
    for k, v in p.items():
        print(f"{k:16} {v}")
    print(f"\ncontrast ink/bg0: {_contrast(p['ink'], p['bg0']):.1f}:1")
    print(f"contrast turquoise_deep/bg0: {_contrast(p['turquoise_deep'], p['bg0']):.1f}:1")
    print(f"contrast sea_deep/bg0: {_contrast(p['sea_deep'], p['bg0']):.1f}:1")
