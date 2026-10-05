#!/usr/bin/env python3
"""
bench_photo_download.py  —  real-time timing of the photo pipeline, before vs after.

You pick photos once in the Google Photos Picker; the same picks (same baseUrls)
then go through both download strategies, so network and album are identical:

  AFTER   all photos at 512px, 8 at a time  ->  score + shortlist  ->  shortlist
          fetched again at 1600px  ->  pick (Gemini per day with --gemini) -> export
  BEFORE  all photos at 1600px, one at a time (the old code)  ->  score

AFTER runs first, so BEFORE's 1600px downloads of shortlisted photos may come
from Google's cache - if anything that flatters BEFORE, never AFTER.

Needs a trip spec (days + places) - e.g. the spec.json of a trip you created in
the local webapp from your prompt + trip document:

  python tools/bench_photo_download.py --spec webapp_data/<trip id>/spec.json
  python tools/bench_photo_download.py --spec ... --gemini     # + real per-day picks

Uses credentials.json / token.json (the CLI's own OAuth). Works in a temp
folder (deleted at the end unless --keep); never touches webapp_data/.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import fetch_photos as fp  # noqa: E402
import select_photos as sp  # noqa: E402

TMP_PREFIX = "memotrip-bench-"


class Timer:
    def __init__(self):
        self.stages: dict[str, float] = {}

    def wrap(self, name, fn):
        def timed(*a, **k):
            t = time.perf_counter()
            try:
                return fn(*a, **k)
            finally:
                self.stages[name] = self.stages.get(name, 0.0) + time.perf_counter() - t

        return timed

    def run(self, name, fn, *a, **k):
        return self.wrap(name, fn)(*a, **k)


def folder_mb(d: pathlib.Path) -> float:
    return sum(p.stat().st_size for p in d.rglob("*.jpg")) / 1e6


def quiet(fn):
    """Silence fetch_photos' one-line-per-photo output while timing."""

    def run(*a, **k):
        saved = fp.print if hasattr(fp, "print") else None
        fp.print = lambda *x, **y: None
        try:
            return fn(*a, **k)
        finally:
            if saved is None:
                del fp.print
            else:
                fp.print = saved

    return run


def run_after(http, items, spec, work, use_gemini, model) -> tuple[Timer, dict]:
    t = Timer()
    gdir = work / "after" / "gphotos"
    t.run("download 512 (8 parallel)", quiet(fp.download), http, items, gdir, 512)
    photos = sp.load_from_manifest(gdir / "manifest.json", gdir)

    def fetch_full(need):
        got = quiet(fp.upgrade)(http, gdir, [p.path.name for p in need], sp.PAGE_PX)
        for p in need:
            p.full_path = got.get(p.path.name)

    # time the real select() stage by stage by wrapping what it calls
    orig = sp.analyse, sp.pick_with_gemini, sp.pick_hero_with_gemini, sp.export
    sp.analyse = t.wrap("score (CV) on 512", sp.analyse)
    sp.pick_with_gemini = t.wrap("Gemini picks (per day)", sp.pick_with_gemini)
    sp.pick_hero_with_gemini = t.wrap("Gemini picks (per day)", sp.pick_hero_with_gemini)
    sp.export = t.wrap("export page photos", sp.export)
    counts = {}
    try:
        start = time.perf_counter()
        sp.select(
            photos,
            json.loads(json.dumps(spec)),  # a copy - the real spec is never touched
            work / "after" / "images" / "trips",
            use_ai=use_gemini,
            model=model,
            fetch_full=t.wrap("download shortlist 1600 (8 parallel)", fetch_full),
            log=lambda m: print("    " + m),
        )
        t.stages["select total"] = time.perf_counter() - start
    finally:
        sp.analyse, sp.pick_with_gemini, sp.pick_hero_with_gemini, sp.export = orig
    full = [p for p in photos if p.full_path]
    counts = {
        "photos": len(photos),
        "shortlist_1600": len(full),
        "mb_512": (
            folder_mb(gdir) - folder_mb(gdir / "full")
            if (gdir / "full").exists()
            else folder_mb(gdir)
        ),
        "mb_1600": folder_mb(gdir / "full") if (gdir / "full").exists() else 0.0,
    }
    return t, counts


def run_before(http, items, work) -> tuple[Timer, dict]:
    t = Timer()
    gdir = work / "before" / "gphotos"
    t.run("download 1600 (serial)", quiet(fp.download), http, items, gdir, 1600, workers=1)
    photos = sp.load_from_manifest(gdir / "manifest.json", gdir)
    t.run("score (CV) on 1600", sp.analyse, photos)
    return t, {"photos": len(photos), "mb_1600": folder_mb(gdir)}


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--spec", required=True, help="trip spec.json (days + places)")
    ap.add_argument("--gemini", action="store_true", help="real per-day Gemini picks in AFTER")
    ap.add_argument("--model", default="gemini-3.6-flash")
    ap.add_argument("--skip-before", action="store_true", help="only time AFTER")
    ap.add_argument("--keep", action="store_true", help="keep the downloaded photos")
    ap.add_argument("--credentials", default="credentials.json")
    ap.add_argument("--token", default="token.json")
    ap.add_argument("--out", default=None, help="also write the numbers as JSON here")
    a = ap.parse_args()

    spec = json.loads((ROOT / a.spec).read_text(encoding="utf-8"))
    http = fp.authorise(ROOT / a.credentials, ROOT / a.token)
    session = fp._ok(http.post(f"{fp.BASE}/sessions", json={})).json()
    work = pathlib.Path(tempfile.mkdtemp(prefix=TMP_PREFIX))
    try:
        fp.wait_for_pick(http, session)
        items = [it for it in fp.list_items(http, session["id"]) if it.get("type") == "PHOTO"]
        print(f"\n  {len(items)} photo(s) picked. Working in {work}\n")

        print("  AFTER (512 for all, 1600 for the shortlist, parallel) ...")
        after, ac = run_after(http, items, spec, work, a.gemini, a.model)
        before, bc = (None, None)
        if not a.skip_before:
            print("\n  BEFORE (1600 for all, serial) ...")
            before, bc = run_before(http, items, work)
    finally:
        fp.close_session(http, session["id"])

    print(f"\n=== {ac['photos']} photos ===")
    print("\nAFTER")
    for k, v in after.stages.items():
        print(f"  {k:<40}{v:>8.1f}s")
    dl_after = after.stages.get("download 512 (8 parallel)", 0) + after.stages.get(
        "download shortlist 1600 (8 parallel)", 0
    )
    print(f"  {'-> downloads total':<40}{dl_after:>8.1f}s")
    print(
        f"  data: {ac['mb_512']:.0f} MB at 512 + {ac['mb_1600']:.0f} MB at 1600 "
        f"({ac['shortlist_1600']} shortlisted)"
    )
    result = {"photos": ac["photos"], "after": {**after.stages, **ac}}
    if before:
        print("\nBEFORE")
        for k, v in before.stages.items():
            print(f"  {k:<40}{v:>8.1f}s")
        print(f"  data: {bc['mb_1600']:.0f} MB at 1600")
        dl_before = before.stages["download 1600 (serial)"]
        print(
            f"\nDOWNLOAD: before {dl_before:.0f}s  ->  after {dl_after:.0f}s  "
            f"({dl_before / dl_after:.1f}x faster);  data {bc['mb_1600']:.0f} MB -> "
            f"{ac['mb_512'] + ac['mb_1600']:.0f} MB"
        )
        result["before"] = {**before.stages, **bc}
    if a.out:
        pathlib.Path(a.out).write_text(json.dumps(result, indent=2), encoding="utf-8")

    if a.keep:
        print(f"\nphotos kept in {work}")
    elif work.name.startswith(TMP_PREFIX) and work.parent == pathlib.Path(tempfile.gettempdir()):
        shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
