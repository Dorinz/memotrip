#!/usr/bin/env python3
"""
bench_photo_tokens.py  —  what does Gemini photo selection cost per trip?

Compares two ways of asking Gemini to pick a trip's photos, on a real album:

  A  per-day   one call per day (its <=12 candidates) + one hero call   <- production today
  B  combined  ONE call: every day's candidates, labelled by day, and the hero
               chosen from photos already in the call (extra hero-only
               candidates are added once)

Input tokens are counted with `count_tokens` for each media resolution
(unspecified/low/medium/high) - free, does not use generate quota. Output +
thinking tokens only exist on a real call, so --live runs strategy B once plus
a sample of A's day calls (and A's hero call), and extrapolates A from the mean.

  python tools/bench_photo_tokens.py                      # input tokens only
  python tools/bench_photo_tokens.py --live               # + real output/thinking
  python tools/bench_photo_tokens.py --live --live-days 0 # all of A's day calls live

Prices are per 1M tokens (gemini-3.6-flash standard, through 2026-12-31;
output includes thinking). Pass --price-in/--price-out to change them.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import statistics
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import select_photos as sp  # noqa: E402  (also loads .env)

RESOLUTIONS = ["UNSPECIFIED", "LOW", "MEDIUM", "HIGH"]

PICKS_SCHEMA = {
    "type": "object",
    "properties": {
        "picks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"index": {"type": "integer"}, "reason": {"type": "string"}},
                "required": ["index"],
            },
        }
    },
    "required": ["picks"],
}

COMBINED_SCHEMA = {
    "type": "object",
    "properties": {
        "days": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "day": {"type": "string"},
                    "picks": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["day", "picks"],
            },
        },
        "hero": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["days", "hero"],
}


# ------------------------------------------------------------------ the calls


def day_instruction(title: str, n: int) -> str:
    # same text as sp.pick_with_gemini
    return (
        f"These are candidate photos for one leg of a trip ('{title}'). "
        f"Choose the {n} best to show together. Favour sharp, well-exposed, "
        f"interesting frames, and make the set varied - different scenes, "
        f"subjects and moments, never near-duplicates. Reply as JSON."
    )


def hero_instruction(n: int) -> str:
    # same text as sp.pick_hero_with_gemini
    return (
        f"These are candidate photos from an entire trip. Choose the {n} most beautiful, "
        f"sweeping SCENERY / landscape shots - the kind that would work as a magazine "
        f"cover or a page's hero background image. Favour wide vistas, striking light, "
        f"dramatic nature or cityscapes. Avoid close-ups of food, documents, indoor detail "
        f"shots, or a photo where a person's face fills the frame. Order picks best first. "
        f"It's completely fine if a pick also belongs to (and will separately appear in) "
        f"its own day's gallery later on the page. Reply as JSON."
    )


def build_per_day(days, hero, hero_n):
    """Strategy A: list of (label, parts, schema) - exactly what production sends."""
    from google.genai import types

    calls = []
    for t, cands, n in days:
        parts = [types.Part.from_text(text=day_instruction(t.title, n))] + sp.candidate_parts(cands)
        calls.append((f"day {t.id}", parts, PICKS_SCHEMA, len(cands)))
    parts = [types.Part.from_text(text=hero_instruction(hero_n))] + sp.candidate_parts(hero)
    calls.append(("hero", parts, PICKS_SCHEMA, len(hero)))
    return calls


def build_combined(days, hero, hero_n):
    """Strategy B: one call. Every photo is sent once, labelled "<day>:<i>"."""
    from google.genai import types

    intro = (
        "You are choosing photos for a trip journal. Below are candidate photos grouped "
        "by day. For EACH day, choose the requested number of photos to show together "
        "for that day: favour sharp, well-exposed, interesting frames, and make each "
        "day's set varied - different scenes, subjects and moments, never near-duplicates. "
        f"Separately, choose the {hero_n} most beautiful, sweeping SCENERY / landscape "
        "shots from the WHOLE trip for the page's hero image (magazine-cover quality: wide "
        "vistas, striking light, dramatic nature or cityscapes; no food close-ups, "
        "documents, indoor details, or faces filling the frame), best first. A hero pick "
        "may also be one of a day's picks. Refer to photos by their label exactly as "
        "written, e.g. capelas-d2:3. Reply as JSON."
    )
    parts = [types.Part.from_text(text=intro)]
    sent = set()
    n_images = 0
    for t, cands, n in days:
        parts.append(types.Part.from_text(text=f"\n== Day {t.id} ('{t.title}') - choose {n} =="))
        parts += sp.candidate_parts(cands, label=lambda i, tid=t.id: f"{tid}:{i}")
        sent.update(p.path for p in cands)
        n_images += len(cands)
    extra = [p for p in hero if p.path not in sent]
    if extra:
        parts.append(types.Part.from_text(text="\n== Extra hero-only candidates =="))
        parts += sp.candidate_parts(extra, label=lambda i: f"hero:{i}")
        n_images += len(extra)
    return [("combined", parts, COMBINED_SCHEMA, n_images)]


def with_resolution(parts, level: str):
    """Copy of `parts` with every image part set to a media resolution."""
    from google.genai import types

    if level == "UNSPECIFIED":
        return parts
    out = []
    for p in parts:
        if p.inline_data is not None:
            p = p.model_copy(
                update={
                    "media_resolution": types.PartMediaResolution(level=f"MEDIA_RESOLUTION_{level}")
                }
            )
        out.append(p)
    return out


# --------------------------------------------------------------------- counting


def count_input(cl, model, parts, level) -> int:
    from google.genai import types

    contents = [types.Content(role="user", parts=with_resolution(parts, level))]
    return cl.models.count_tokens(model=model, contents=contents).total_tokens


def live_call(cl, model, parts, schema) -> dict:
    from google.genai import types

    cfg = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=schema,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )
    t0 = time.time()
    for attempt in range(4):
        try:
            resp = cl.models.generate_content(
                model=model, contents=[types.Content(role="user", parts=parts)], config=cfg
            )
            break
        except Exception as e:  # transient 503s - same idea as gemini_util
            if attempt == 3 or not any(s in str(e) for s in ("503", "UNAVAILABLE", "500")):
                raise
            time.sleep(5 * (attempt + 1))
    u = resp.usage_metadata
    return {
        "in": u.prompt_token_count or 0,
        "out": u.candidates_token_count or 0,
        "think": u.thoughts_token_count or 0,
        "secs": round(time.time() - t0, 1),
        "text": resp.text,
    }


def usd(tokens_in, tokens_out, a) -> float:
    return tokens_in / 1e6 * a.price_in + tokens_out / 1e6 * a.price_out


# ------------------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--spec", default="azores-archive/trip_spec.json")
    ap.add_argument("--manifest", default="azores-archive/gphotos/manifest.json")
    ap.add_argument("--media-dir", default="azores-archive/gphotos")
    ap.add_argument("--model", default="gemini-3.6-flash")
    ap.add_argument("--price-in", type=float, default=0.75, help="USD per 1M input tokens")
    ap.add_argument(
        "--price-out", type=float, default=3.75, help="USD per 1M output+thinking tokens"
    )
    ap.add_argument("--live", action="store_true", help="also make real calls for output/thinking")
    ap.add_argument("--live-days", type=int, default=3, help="A's day calls to run live (0 = all)")
    ap.add_argument("--out", default=None, help="write the raw numbers as JSON here")
    a = ap.parse_args()

    import gemini_util as gu

    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        sys.exit("needs GEMINI_API_KEY")
    cl = gu.client(key)

    spec = json.loads((ROOT / a.spec).read_text(encoding="utf-8"))
    photos = sp.load_from_manifest(ROOT / a.manifest, ROOT / a.media_dir)
    targets = sp.load_targets(spec)
    sp.analyse(photos)
    buckets = sp.bucket(photos, targets)

    # exactly production's shortlist: only days where Gemini actually gets called
    days = []
    for t in targets:
        pics = buckets.get(t.id, [])
        if not pics:
            continue
        cands, n = sp.day_candidates(pics)
        if len(cands) > n:
            days.append((t, cands, n))
    hero_n = 2
    hero = sp.hero_pool(photos, 12, hero_n)

    A = build_per_day(days, hero, hero_n)
    B = build_combined(days, hero, hero_n)
    a_images = sum(c[3] for c in A)
    b_images = B[0][3]
    print(f"album: {len(photos)} photos, {len(targets)} days, {len(days)} days get a Gemini call")
    print(f"A per-day : {len(A)} calls, {a_images} images sent")
    print(f"B combined: 1 call,  {b_images} images sent (each photo once)\n")

    result = {
        "photos": len(photos),
        "days_called": len(days),
        "A_calls": len(A),
        "A_images": a_images,
        "B_images": b_images,
        "input": {},
        "live": {},
    }

    print("INPUT TOKENS (count_tokens)")
    print(f"{'resolution':<12}{'A total':>10}{'B total':>10}{'B vs A':>9}{'tok/img':>9}")
    for level in RESOLUTIONS:
        a_tok = sum(count_input(cl, a.model, c[1], level) for c in A)
        b_tok = count_input(cl, a.model, B[0][1], level)
        per_img = (b_tok - count_input(cl, a.model, [B[0][1][0]], level)) / b_images
        result["input"][level] = {"A": a_tok, "B": b_tok, "per_image": round(per_img)}
        print(
            f"{level:<12}{a_tok:>10,}{b_tok:>10,}{(b_tok / a_tok - 1) * 100:>8.0f}%{per_img:>9.0f}"
        )

    if a.live:
        print("\nLIVE CALLS (default resolution, as production)")
        sample = [c for c in A if c[0] != "hero"]
        sample = sample if a.live_days <= 0 else sample[: a.live_days]
        hero_call = A[-1]
        rows = []
        for label, parts, schema, n_img in sample + [hero_call]:
            r = live_call(cl, a.model, parts, schema)
            rows.append((label, r))
            print(
                f"  A {label:<22} in {r['in']:>6,}  out {r['out']:>5,}  think {r['think']:>5,}  {r['secs']:>5}s"
            )
        rb = live_call(cl, a.model, B[0][1], B[0][2])
        print(
            f"  B {'combined':<22} in {rb['in']:>6,}  out {rb['out']:>5,}  think {rb['think']:>5,}  {rb['secs']:>5}s"
        )

        day_rows = [r for lbl, r in rows if lbl != "hero"]
        hero_r = rows[-1][1]
        n_day_calls = len(A) - 1
        mean_out = statistics.mean(r["out"] + r["think"] for r in day_rows)
        mean_secs = statistics.mean(r["secs"] for r in day_rows)
        a_in = result["input"]["UNSPECIFIED"]["A"]
        a_out = mean_out * n_day_calls + hero_r["out"] + hero_r["think"]
        a_secs = mean_secs * n_day_calls + hero_r["secs"]
        b_in, b_out = rb["in"], rb["out"] + rb["think"]
        extrap = (
            ""
            if len(day_rows) == n_day_calls
            else f" (day calls extrapolated from {len(day_rows)})"
        )
        print(f"\nPER TRIP{extrap}")
        print(f"{'':<12}{'calls':>6}{'input':>10}{'out+think':>11}{'USD':>10}{'serial s':>10}")
        print(
            f"{'A per-day':<12}{len(A):>6}{a_in:>10,}{a_out:>11,.0f}{usd(a_in, a_out, a):>10.4f}{a_secs:>10.0f}"
        )
        print(
            f"{'B combined':<12}{1:>6}{b_in:>10,}{b_out:>11,}{usd(b_in, b_out, a):>10.4f}{rb['secs']:>10.0f}"
        )
        result["live"] = {
            "A_rows": [(l, {k: v for k, v in r.items() if k != "text"}) for l, r in rows],
            "B": {k: v for k, v in rb.items() if k != "text"},
            "B_text": rb["text"],
            "A_est": {"in": a_in, "out": a_out, "secs": a_secs, "usd": usd(a_in, a_out, a)},
            "B_est": {"in": b_in, "out": b_out, "secs": rb["secs"], "usd": usd(b_in, b_out, a)},
        }

    if a.out:
        pathlib.Path(a.out).write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
