#!/usr/bin/env python3
"""
build_trip.py — render a trip-journal HTML page from a trip_spec.json + trip_template.html.

Phase 1 of the "trip journal generator": proves the page is fully data-driven.
The template holds all design/CSS/JS; trip_spec.json holds every trip-specific string,
coordinate, photo path and timeline entry.

Usage:
    python build_trip.py                                  # spec=trip_spec.json, out=dist/trip.html
    python build_trip.py --spec other.json --out page.html
"""
import argparse
import json
import pathlib
import re
import sys

import palette


def _stats(items, indent):
    pad = " " * indent
    return "\n".join(
        f'{pad}<div><div class="n">{s["n"]}</div><div class="l">{s["l"]}</div></div>'
        for s in items
    )


def _econ_big(items):
    return "\n".join(
        f'      <div class="bn"><div class="v">{b["v"]}</div><div class="k">{b["k"]}</div></div>'
        for b in items
    )


def _econ_bars(items):
    return "\n".join(
        f'      <div class="row"><div class="bh"><span>{b["label"]}</span>'
        f'<span class="amt">{b["amt"]}</span></div>'
        f'<div class="track"><div class="fill" style="width:{b["width"]}%"></div></div></div>'
        for b in items
    )


def _econ_notes(items):
    return "\n".join(
        f'      <div class="fn"><span class="fn-k">{n["k"]}</span>{n["html"]}</div>'
        for n in items
    )


def _json_block(obj):
    # valid JS object/array literal, readable, matches the template's inline style
    return json.dumps(obj, ensure_ascii=False, indent=2)


def build(spec: dict, template: str) -> str:
    g = lambda d, k, dflt="": (d or {}).get(k, dflt)
    hero = spec.get("hero") or {}
    outro = spec.get("outro") or {}
    econ = spec.get("economics") or {}
    theme = spec.get("theme") or palette.DEFAULT
    out = template

    # economics is a bonus section — drop it entirely when the spec has no economics data
    if not (econ.get("bars") or econ.get("big") or econ.get("intro")):
        out = re.sub(r"\n<!-- =+ ECONOMICS.*?</section>\n", "\n", out, flags=re.S)

    repl = {
        "{{TITLE}}": g(spec.get("meta"), "title", "Trip Journal"),

        "{{HERO_EYEBROW}}": g(hero, "eyebrow"),
        "{{HERO_H1}}": g(hero, "h1"),
        "{{HERO_SUB}}": g(hero, "sub"),
        "{{HERO_META}}": _stats(hero.get("meta") or [], 4),

        "{{OUTRO_EYEBROW}}": g(outro, "eyebrow"),
        "{{OUTRO_H2}}": g(outro, "h2"),
        "{{OUTRO_P}}": g(outro, "p"),
        "{{OUTRO_STATS}}": _stats(outro.get("stats") or [], 6),

        "{{ECON_EYEBROW}}": g(econ, "eyebrow"),
        "{{ECON_H2}}": g(econ, "h2"),
        "{{ECON_INTRO}}": g(econ, "intro"),
        "{{ECON_BIG}}": _econ_big(econ.get("big") or []),
        "{{ECON_BARS}}": _econ_bars(econ.get("bars") or []),
        "{{ECON_NOTES}}": _econ_notes(econ.get("notes") or []),
        "{{ECON_FOOT}}": g(econ, "foot"),

        "{{C_BG0}}": theme["bg0"], "{{C_BG1}}": theme["bg1"], "{{C_BG3}}": theme["bg3"],
        "{{C_INK}}": theme["ink"], "{{C_INK_DIM}}": theme["ink_dim"], "{{C_MUTED}}": theme["muted"],
        "{{C_TURQ}}": theme["turquoise"], "{{C_TURQ_DEEP}}": theme["turquoise_deep"],
        "{{C_SEA}}": theme["sea"], "{{C_SEA_DEEP}}": theme["sea_deep"],
        "{{C_INK_RGB}}": theme["ink_rgb"], "{{C_TURQ_RGB}}": theme["turquoise_rgb"],
        "{{C_SEA_RGB}}": theme["sea_rgb"],

        "{{LOCATIONS_JSON}}": _json_block(spec.get("locations") or {}),
        "{{PHOTOS_JSON}}": _json_block(spec.get("photos") or {}),
        "{{TIMELINE_JSON}}": _json_block(spec.get("timeline") or []),
    }

    for key, value in repl.items():
        out = out.replace(key, value)

    left = [tok for tok in ("{{", "}}") if tok in out]
    if left:
        raise SystemExit("unfilled placeholder markers remain in output")
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--spec", default="trip_spec.json")
    ap.add_argument("--template", default="trip_template.html")
    ap.add_argument("--out", default="azores-trip.html",
                    help="write here; keep it beside the images/ folder so photo paths resolve")
    args = ap.parse_args()

    root = pathlib.Path(__file__).resolve().parent
    spec_path = (root / args.spec) if not pathlib.Path(args.spec).is_absolute() else pathlib.Path(args.spec)
    tpl_path = (root / args.template) if not pathlib.Path(args.template).is_absolute() else pathlib.Path(args.template)
    out_path = (root / args.out) if not pathlib.Path(args.out).is_absolute() else pathlib.Path(args.out)

    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    template = tpl_path.read_text(encoding="utf-8")

    html = build(spec, template)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # force LF regardless of platform so output is byte-stable across machines
    out_path.write_bytes(html.replace("\r\n", "\n").encode("utf-8"))

    print(f"built {out_path}  ({len(html):,} chars, {len(spec['timeline'])} timeline items)")


if __name__ == "__main__":
    sys.exit(main())
