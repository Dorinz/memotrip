#!/usr/bin/env python3
"""
gen_copy.py  —  Phase 3, step 2 of the trip-journal generator.

Fills the *prose* of a trip_spec with Gemini: per-day title / lede / about /
tags (about + lodging framing only on check-in days), the hero and outro
blocks, and the disruption ("drama") text. Given the structured timeline
(from parse_docs.py's expand_days() or hand-built) + a free-text trip
description (+ optionally the raw logistics docs for activity detail).

Requires GEMINI_API_KEY. Two API calls total: one trip-level, one for all days.
By default it only fills empty fields and writes to trip_spec.copy.json for you
to review; --overwrite regenerates everything, --in-place edits the spec.

    python gen_copy.py --description trip.txt
    python gen_copy.py --spec trip_spec.draft.json --description "היינו שבועיים..." --docs "*.pdf"
    python gen_copy.py --description trip.txt --overwrite --in-place
    python gen_copy.py --description trip.txt --dry-run
"""
from __future__ import annotations

try:
    import local_env  # noqa: F401  (loads .env)
except Exception:
    pass

import argparse
import json
import os
import pathlib
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


# ---------------------------------------------------------------- doc text (reuse)

def read_docs(patterns: list[str], root: pathlib.Path) -> str:
    if not patterns:
        return ""
    import glob
    try:
        from parse_docs import read_doc                       # same extractors
    except Exception:
        return ""
    files: list[pathlib.Path] = []
    for pat in patterns:
        files += [pathlib.Path(x) for x in glob.glob(str(root / pat))]
    return "\n\n".join(read_doc(f) for f in dict.fromkeys(files) if f.is_file())


# --------------------------------------------------------------------- itinerary digest

def digest(spec: dict) -> str:
    """A compact plain-text summary of the timeline for the model to reason over."""
    out = []
    for i, it in enumerate(spec.get("timeline", [])):
        t = it.get("type")
        if t == "transit":
            out.append(f"{i}. TRANSIT {it.get('mode','')} {it.get('date','')}: "
                       f"{it.get('from','')} -> {it.get('to','')}  {it.get('route','')}".rstrip())
        elif t == "drama":
            out.append(f"{i}. DRAMA (flight disruption): {it.get('from','')} <-> {it.get('to','')}")
        elif t == "day":
            out.append(f"{i}. DAY key={it.get('key','')} day={it.get('dayIndex')}/{it.get('dayCount')} "
                       f"{it.get('city','')}, {it.get('island','')}  {it.get('date','')}"
                       + ("  CHECK-IN" if it.get("isCheckIn") else "")
                       + (f"  host={it.get('host','')}" if it.get('host') else ""))
        elif t == "layover":
            rng = it.get("dateRange") or it.get("dates") or ""
            out.append(f"{i}. LAYOVER key={it.get('key','')} "
                       f"{it.get('city','')}, {it.get('island','')}  {rng}")
    return "\n".join(out)


# --------------------------------------------------------------------- gemini

def _client(model: str):
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        sys.exit("gen_copy needs GEMINI_API_KEY in the environment (prose can't be generated offline).")
    import logging
    from google import genai
    from google.genai import types
    logging.getLogger("google_genai").setLevel(logging.ERROR)
    return genai.Client(api_key=api_key), types


def _ask(client, types, model: str, prompt: str, schema: dict, temperature: float) -> dict:
    resp = client.models.generate_content(
        model=model, contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json", response_schema=schema,
            temperature=temperature,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True)),
    )
    return json.loads(resp.text)


TRIP_SCHEMA = {
    "type": "object",
    "properties": {
        "meta": {"type": "object", "properties": {"title": {"type": "string"}}},
        "hero": {"type": "object", "properties": {
            "eyebrow": {"type": "string"}, "h1": {"type": "string"}, "sub": {"type": "string"},
            "region": {"type": "string"},
            "meta": {"type": "array", "items": {"type": "object", "properties": {
                "n": {"type": "string"}, "l": {"type": "string"}}, "required": ["n", "l"]}},
        }, "required": ["eyebrow", "h1", "sub", "region", "meta"]},
        "outro": {"type": "object", "properties": {
            "eyebrow": {"type": "string"}, "h2": {"type": "string"}, "p": {"type": "string"},
            "stats": {"type": "array", "items": {"type": "object", "properties": {
                "n": {"type": "string"}, "l": {"type": "string"}}, "required": ["n", "l"]}},
        }, "required": ["eyebrow", "h2", "p", "stats"]},
        "drama": {"type": "object", "properties": {
            "eyebrow": {"type": "string"},
            "legs": {"type": "array", "items": {"type": "string"}},
            "paras": {"type": "array", "items": {"type": "string"}},
            "badge": {"type": "string"},
        }},
    },
    "required": ["meta", "hero", "outro"],
}

DAYS_SCHEMA = {
    "type": "object",
    "properties": {"days": {"type": "array", "items": {"type": "object", "properties": {
        "key": {"type": "string"}, "dayIndex": {"type": "integer"},
        "title": {"type": "string"},        # check-in: small line above "City, Island"; else: the big heading
        "island": {"type": "string"},       # fill if missing (check-in days only)
        "lede": {"type": "string"},          # 1-2 sentences on THAT day
        "about": {"type": "string"},         # check-in days only: one fact about the place + why
        "tags": {"type": "array", "items": {"type": "string"}},   # 2-4 short activity chips for that day
    }, "required": ["key", "dayIndex", "title", "lede", "tags"]}}},
    "required": ["days"],
}

TRIP_PROMPT = """Write the framing copy for a personal trip-journal web page, in {lang}.
Voice: first person plural, warm, concrete, spoken — not marketing. No clichés.

Trip description from the traveller:
{description}

Itinerary:
{digest}

Produce JSON:
- meta.title: a short page title.
- hero.eyebrow: dates + a one-line hook. hero.h1: 2-4 words, may wrap one word in
  <em></em>. hero.sub: two short sentences; put <br> between them. hero.region:
  2-4 words naming just the overall destination/region (e.g. "the Azores
  islands"), general — not an itinerary detail or a specific place visited.
  hero.meta: 4-5 {{n,l}} stat chips computed from the itinerary (islands /
  countries, lodgings, flights, ferries, nights).
- outro.eyebrow, outro.h2 (2-4 words), outro.p (2-3 sentences, the emotional
  close), outro.stats: 3 {{n,l}} chips.
{drama_line}
Return only the JSON."""

DRAMA_LINE = ("- drama: the itinerary has a flight disruption. eyebrow (date + what "
              "happened + flight no. if known); legs (3 place names, there-and-back); "
              "paras (2 short spoken paragraphs telling the story); badge (a one-line "
              "emoji summary with arrows).")

DAYS_PROMPT = """For each day below, write journal copy in {lang}.
Voice: first person plural, warm, concrete, spoken - vary the phrasing, never
reuse the same sentence shape or fact across two days.

Trip description:
{description}

Day-by-day / logistics notes (align by date - these often already spell out
what happened on a specific day, e.g. "יום שלישי 4/8: ..."):
{docs}

Days (keep the same key + dayIndex; chronological; CHECK-IN marks arrival day
at that lodging):
{days}

For each day return {{key, dayIndex, title, island, lede, about, tags}}:
- title: short line. On a CHECK-IN day it sits SMALL above a big "City, Island"
  heading - e.g. "לילה ראשון, תחנת מעבר" or "יומיים באי החום" (weave in the
  island's nickname if one is well known). On any OTHER day it IS the big
  heading (2-5 words, specific to that day's plan, e.g. "בין הכרמים ולשקיעה") -
  the small line above it will just say "City, Island · יום 2 מתוך 3", so make
  it earn its place.
- island: fill ONLY if blank (only matters on CHECK-IN days).
- lede: 1-2 sentences on what we actually did THAT specific day, grounded in
  the notes for that date. Never a generic "we relaxed" filler line.
- about: ONLY on CHECK-IN days - one genuinely interesting fact about the
  place and why (history, geology, a nickname's origin). Leave empty on every
  other day.
- tags: 2-4 very short chips of that day's actual activities, from the notes.
Return only the JSON."""


# --------------------------------------------------------------------- merge

PROSE_DAY = ("title", "island", "lede", "about", "tags")


def fill(dst: dict, src: dict, keys, overwrite: bool) -> list[str]:
    changed = []
    for k in keys:
        v = src.get(k)
        if v in (None, "", [], {}):
            continue
        if overwrite or dst.get(k) in (None, "", [], {}):
            dst[k] = v
            changed.append(k)
    return changed


def generate_copy(spec: dict, description: str, docs_text: str = "", *,
                  model: str = "gemini-3.6-flash", lang: str = "he",
                  overwrite: bool = False, log=print) -> dict:
    """Fill spec's prose in place (and return it). Raises if GEMINI_API_KEY is missing."""
    days = [it for it in spec.get("timeline", []) if it.get("type") in ("day", "layover")]
    has_drama = any(it.get("type") == "drama" for it in spec.get("timeline", []))

    trip_prompt = TRIP_PROMPT.format(
        lang=lang, description=description.strip(), digest=digest(spec),
        drama_line=DRAMA_LINE if has_drama else "")
    days_blob = "\n".join(
        f"- key={d.get('key')} dayIndex={d.get('dayIndex', 1)}  "
        f"{d.get('city','')}, {d.get('island','') or '?'}  {d.get('date') or (d.get('dateRange') or [''])[0]}"
        + ("  CHECK-IN" if d.get("isCheckIn", d.get("type") == "layover") else "")
        for d in days)
    days_prompt = DAYS_PROMPT.format(
        lang=lang, description=description.strip(),
        docs=(docs_text[:20000] or "(none provided)"), days=days_blob)

    import gemini_util as gu
    cl = gu.client()
    fails = []

    try:
        log("trip-level copy ...")
        trip = gu.generate_json(model, trip_prompt, schema=TRIP_SCHEMA, temperature=0.7, cl=cl, log=log)
        spec.setdefault("meta", {})
        fill(spec["meta"], trip.get("meta", {}), ("title",), overwrite)
        for block in ("hero", "outro"):
            spec.setdefault(block, {})
            fill(spec[block], trip.get(block, {}),
                 ("eyebrow", "h1", "h2", "sub", "p", "region", "meta", "stats"), overwrite)
        if has_drama and trip.get("drama"):
            for it in spec["timeline"]:
                if it.get("type") == "drama":
                    fill(it, trip["drama"], ("eyebrow", "legs", "paras", "badge"), overwrite)
    except Exception as e:
        fails.append(f"trip-level copy failed: {e}")
        log(fails[-1])

    try:
        log(f"day copy ({len(days)}) ...")
        got = {(d.get("key"), d.get("dayIndex", 1)): d for d in
               gu.generate_json(model, days_prompt, schema=DAYS_SCHEMA, temperature=0.7,
                                cl=cl, log=log).get("days", [])}
        n = 0
        for it in spec["timeline"]:
            if it.get("type") not in ("day", "layover"):
                continue
            src = got.get((it.get("key"), it.get("dayIndex", 1)))
            if src and fill(it, src, PROSE_DAY, overwrite):
                n += 1
        log(f"filled prose on {n} days")
    except Exception as e:
        fails.append(f"day copy failed: {e}")
        log(fails[-1])

    if fails:
        spec.setdefault("_warnings", []).extend(fails)
    return spec


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--spec", default="trip_spec.json")
    ap.add_argument("--description", required=True,
                    help="free-text trip description: a file path or the text itself")
    ap.add_argument("--docs", nargs="*", default=[], help="optional logistics globs for activity detail")
    ap.add_argument("--out", default="trip_spec.copy.json")
    ap.add_argument("--in-place", dest="in_place", action="store_true", help="write back into --spec")
    ap.add_argument("--overwrite", action="store_true", help="regenerate fields that already have text")
    ap.add_argument("--model", default="gemini-3.6-flash")
    ap.add_argument("--lang", default="he")
    ap.add_argument("--dry-run", action="store_true", help="assemble prompts, don't call the API or write")
    a = ap.parse_args()

    root = pathlib.Path(__file__).resolve().parent
    rel = lambda x: pathlib.Path(x) if pathlib.Path(x).is_absolute() else root / x

    spec = json.loads(rel(a.spec).read_text(encoding="utf-8"))
    desc_p = rel(a.description)
    description = desc_p.read_text(encoding="utf-8") if desc_p.exists() else a.description
    docs_text = read_docs(a.docs, root)

    days = [it for it in spec.get("timeline", []) if it.get("type") in ("day", "layover")]
    has_drama = any(it.get("type") == "drama" for it in spec.get("timeline", []))

    trip_prompt = TRIP_PROMPT.format(
        lang=a.lang, description=description.strip(), digest=digest(spec),
        drama_line=DRAMA_LINE if has_drama else "")
    days_blob = "\n".join(
        f"- key={d.get('key')} dayIndex={d.get('dayIndex', 1)}  "
        f"{d.get('city','')}, {d.get('island','') or '?'}  {d.get('date') or (d.get('dateRange') or [''])[0]}"
        + ("  CHECK-IN" if d.get("isCheckIn", d.get("type") == "layover") else "")
        for d in days)
    days_prompt = DAYS_PROMPT.format(
        lang=a.lang, description=description.strip(),
        docs=(docs_text[:20000] or "(none provided)"), days=days_blob)

    if a.dry_run:
        print("=== TRIP-LEVEL PROMPT ===\n" + trip_prompt)
        print("\n=== DAYS PROMPT ===\n" + days_prompt)
        print(f"\n[dry-run] would fill prose for {len(days)} days"
              + (" + a drama block" if has_drama else "")
              + f"; overwrite={a.overwrite}; write -> "
              + (a.spec if a.in_place else a.out))
        return 0

    generate_copy(spec, description, docs_text,
                  model=a.model, lang=a.lang, overwrite=a.overwrite,
                  log=lambda m: print("· " + m))

    dst = rel(a.spec) if a.in_place else rel(a.out)
    dst.write_bytes((json.dumps(spec, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    print(f"\nwrote {dst.name}")
    if not a.in_place:
        print("  review it, then copy the good parts into trip_spec.json (or re-run with --in-place)")
    print("  then:  python build_trip.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
