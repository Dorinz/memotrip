#!/usr/bin/env python3
"""
select_photos.py  —  Phase 2, step 2 of the trip-journal generator.

Takes a folder of trip photos (from fetch_photos.py, or a manual album download),
buckets them by date into the trip's stays, throws out the blurry / near-duplicate
ones, then picks the best few per stay -- with Gemini if a key is available, or a
deterministic quality+diversity fallback otherwise. Chosen photos are resized into
images/trips/<key>-N.jpg and wired into trip_spec.json ("trip" arrays only).

Run  build_trip.py  afterwards to regenerate the page.

Examples
--------
  # photos fetched by fetch_photos.py
  python select_photos.py --manifest gphotos/manifest.json --media-dir gphotos

  # a plain folder (e.g. Google Photos "Download all"); dates read from EXIF
  python select_photos.py --source folder --folder "C:/Users/me/Downloads/azores"

  # see the plan without writing anything
  python select_photos.py --source folder --folder ./album --dry-run

Needs: Pillow (always). google-genai only for the --ai path.
"""
from __future__ import annotations

try:
    import local_env  # noqa: F401  (loads .env)
except Exception:
    pass

import argparse
import base64
import datetime as dt
import io
import json
import os
import pathlib
import sys
from dataclasses import dataclass, field

try:
    from PIL import Image, ImageOps, ImageStat, ImageFilter
except ImportError:
    sys.exit("Pillow is required:  pip install Pillow")

# never die on a console that can't encode Hebrew place names
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".tif", ".tiff"}
EXIF_DATETIME_ORIGINAL = 36867
EXIF_DATETIME = 306


# --------------------------------------------------------------------------- data

@dataclass
class Photo:
    path: pathlib.Path
    taken: dt.datetime | None            # naive UTC (best effort)
    width: int = 0
    height: int = 0
    # filled in during analysis
    sharp: float = 0.0
    expo_pen: float = 0.0
    score: float = 0.0
    dhash: int = 0
    bucket: str | None = None


@dataclass
class Target:
    """One photo-gallery slot: a single calendar day (a `day` timeline item) or
    a one-off `layover`. Each calendar date maps to exactly one Target."""
    id: str                 # output id, e.g. "capelas-d2" or "lisbon_layover"
    date: dt.date
    title: str = ""
    kind: str = "day"        # "day" | "layover"
    item: dict | None = None    # the `day` item to write .tripPhotos back onto
    key: str = ""             # for "layover": which spec["photos"][key]["trip"] to fill


# ----------------------------------------------------------------------- loading

def _parse_iso(s: str) -> dt.datetime | None:
    if not s:
        return None
    s = s.strip().replace("Z", "+00:00")
    try:
        d = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    if d.tzinfo is not None:
        d = d.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return d


def _exif_datetime(img: Image.Image) -> dt.datetime | None:
    try:
        ex = img.getexif()
    except Exception:
        return None
    for tag in (EXIF_DATETIME_ORIGINAL, EXIF_DATETIME):
        v = ex.get(tag)
        if isinstance(v, str) and len(v) >= 19:
            try:
                return dt.datetime.strptime(v[:19], "%Y:%m:%d %H:%M:%S")
            except ValueError:
                pass
    return None


def load_from_manifest(manifest: pathlib.Path, media_dir: pathlib.Path) -> list[Photo]:
    data = json.loads(manifest.read_text(encoding="utf-8"))
    out: list[Photo] = []
    for it in data.get("items", []):
        p = pathlib.Path(it.get("file") or "")
        if not p.is_absolute():
            p = media_dir / p.name
        if not p.exists():
            print(f"  ! missing file for manifest item: {p}", file=sys.stderr)
            continue
        # dims are read from the file in analyse(); orig_* (pre-downscale) drives the
        # resolution bonus so a downscaled 12MP shot still outranks a downscaled phone crop
        out.append(Photo(
            path=p,
            taken=_parse_iso(it.get("createTime", "")),
            width=int(it.get("orig_width") or it.get("width") or 0),
            height=int(it.get("orig_height") or it.get("height") or 0),
        ))
    return out


def load_from_folder(folder: pathlib.Path) -> list[Photo]:
    out: list[Photo] = []
    for p in sorted(folder.rglob("*")):
        if p.suffix.lower() not in IMG_EXTS or not p.is_file():
            continue
        taken, w, h = None, 0, 0
        try:
            with Image.open(p) as im:
                w, h = im.size
                taken = _exif_datetime(im)
        except Exception as e:
            print(f"  ! cannot read {p.name}: {e}", file=sys.stderr)
            continue
        if taken is None:
            taken = dt.datetime.fromtimestamp(p.stat().st_mtime)
        out.append(Photo(path=p, taken=taken, width=w, height=h))
    return out


def load_targets(spec: dict) -> list[Target]:
    """One target per `day` item (by its exact date) and per `layover` (by
    dateRange[0]). Since the spec is already day-expanded, dates don't overlap."""
    targets: list[Target] = []
    for item in spec.get("timeline", []):
        if item.get("type") == "day" and item.get("date"):
            targets.append(Target(
                id=f'{item["key"]}-d{item["dayIndex"]}', date=dt.date.fromisoformat(item["date"]),
                title=item.get("title") or item.get("city") or item["key"],
                kind="day", item=item, key=item["key"]))
        elif item.get("type") == "layover":
            rng = item.get("dateRange")
            if isinstance(rng, list) and rng and rng[0]:
                targets.append(Target(
                    id=item["key"], date=dt.date.fromisoformat(rng[0]),
                    title=item.get("title") or item.get("city") or item["key"],
                    kind="layover", key=item["key"]))
            else:
                print(f"  ! {item.get('key')} has no dateRange - skipped", file=sys.stderr)
    return targets


# --------------------------------------------------------------------- analysis

def _prep_gray(img: Image.Image, box: int = 512) -> Image.Image:
    img = ImageOps.exif_transpose(img)
    img = img.convert("L")
    img.thumbnail((box, box))
    return img


def sharpness(gray: Image.Image) -> float:
    """Variance of the Laplacian - higher means more in-focus detail."""
    lap = gray.filter(ImageFilter.Kernel((3, 3), [0, 1, 0, 1, -4, 1, 0, 1, 0], scale=1, offset=128))
    return ImageStat.Stat(lap).var[0]


def exposure_penalty(gray: Image.Image) -> float:
    """0 = fine; grows as the frame is mostly crushed black or blown white or flat."""
    h = gray.histogram()
    total = sum(h) or 1
    crushed = sum(h[:6]) / total
    blown = sum(h[250:]) / total
    stdev = ImageStat.Stat(gray).stddev[0]
    flat = max(0.0, (28.0 - stdev) / 28.0)          # low-contrast frames
    return min(1.5, crushed * 2.2 + blown * 2.2 + flat)


def dhash(gray9x8: Image.Image) -> int:
    g = gray9x8.resize((9, 8)).convert("L")
    px = g.tobytes()                      # 72 bytes, one grey level per pixel
    bits = 0
    for row in range(8):
        for col in range(8):
            left = px[row * 9 + col]
            right = px[row * 9 + col + 1]
            bits = (bits << 1) | (1 if left > right else 0)
    return bits


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def analyse(photos: list[Photo]) -> None:
    for p in photos:
        try:
            with Image.open(p.path) as im:
                if not p.width:
                    p.width, p.height = im.size
                g = _prep_gray(im, 512)
                p.sharp = sharpness(g)
                p.expo_pen = exposure_penalty(g)
                p.dhash = dhash(g)
        except Exception as e:
            print(f"  ! analysis failed for {p.path.name}: {e}", file=sys.stderr)
            p.sharp, p.expo_pen = 0.0, 2.0

    if photos:
        smax = max((p.sharp for p in photos), default=1.0) or 1.0
        for p in photos:
            mp = (p.width * p.height) / 1_000_000
            res_bonus = min(0.25, mp / 48.0)
            p.score = max(0.0, (p.sharp / smax) - p.expo_pen * 0.6 + res_bonus)


# ---------------------------------------------------------------------- bucketing

def bucket(photos: list[Photo], targets: list[Target], tz_offset_h: float) -> dict[str, list[Photo]]:
    by_date: dict[dt.date, Target] = {t.date: t for t in targets}   # day-expansion -> no overlaps
    by_id: dict[str, list[Photo]] = {t.id: [] for t in targets}
    unmatched = 0
    off = dt.timedelta(hours=tz_offset_h)
    for p in photos:
        if p.taken is None:
            unmatched += 1
            continue
        d = (p.taken + off).date()
        hit = by_date.get(d)
        if hit is None:
            unmatched += 1
            continue
        p.bucket = hit.id
        by_id[hit.id].append(p)
    if unmatched:
        print(f"  {unmatched} photo(s) fell on a date with no matching day/layover - ignored")
    return by_id


# ------------------------------------------------------------------- de-dup + pick

def dedupe(cands: list[Photo], max_dist: int) -> list[Photo]:
    kept: list[Photo] = []
    for p in sorted(cands, key=lambda x: x.score, reverse=True):
        if all(hamming(p.dhash, k.dhash) > max_dist for k in kept):
            kept.append(p)
    return kept


def pick_diverse(cands: list[Photo], n: int, spread_dist: int) -> list[Photo]:
    """Greedy: best first, then the best remaining that is visually far from all picks."""
    pool = sorted(cands, key=lambda x: x.score, reverse=True)
    picks: list[Photo] = []
    for p in pool:
        if len(picks) >= n:
            break
        if all(hamming(p.dhash, q.dhash) >= spread_dist for q in picks):
            picks.append(p)
    if len(picks) < n:                       # loosen if we came up short
        for p in pool:
            if len(picks) >= n:
                break
            if p not in picks:
                picks.append(p)
    return picks[:n]


def _gemini_pick_raw(cands: list[Photo], n: int, model: str, api_key: str,
                     instruction: str) -> list[Photo] | None:
    """Shared Gemini call: send `instruction` + every candidate as a labelled
    thumbnail, get back an ordered list of picks (best first)."""
    try:
        import gemini_util as gu
        from google.genai import types
    except ImportError:
        print("  (google-genai not installed - using the offline picker)")
        return None
    try:
        cl = gu.client(api_key)
        parts = [types.Part.from_text(text=instruction)]
        for i, p in enumerate(cands):
            with Image.open(p.path) as im:
                im = ImageOps.exif_transpose(im).convert("RGB")
                im.thumbnail((512, 512))
                buf = io.BytesIO()
                im.save(buf, "JPEG", quality=80)
            parts.append(types.Part.from_text(text=f"[{i}]"))
            parts.append(types.Part.from_bytes(data=buf.getvalue(), mime_type="image/jpeg"))
        schema = {
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
        data = gu.generate_json(model, [types.Content(role="user", parts=parts)],
                                schema=schema, temperature=0.4, cl=cl)
        idx = [int(x["index"]) for x in data.get("picks", []) if 0 <= int(x["index"]) < len(cands)]
        seen, ordered = set(), []
        for i in idx:
            if i not in seen:
                seen.add(i)
                ordered.append(cands[i])
        return ordered[:n] if ordered else None
    except Exception as e:
        print(f"  (Gemini pick failed: {e} - using the offline picker)")
        return None


def pick_with_gemini(cands: list[Photo], n: int, model: str, api_key: str,
                     stay_title: str) -> list[Photo] | None:
    instruction = (
        f"These are candidate photos for one leg of a trip ('{stay_title}'). "
        f"Choose the {n} best to show together. Favour sharp, well-exposed, "
        f"interesting frames, and make the set varied - different scenes, "
        f"subjects and moments, never near-duplicates. Reply as JSON.")
    return _gemini_pick_raw(cands, n, model, api_key, instruction)


def pick_hero_with_gemini(cands: list[Photo], n: int, model: str, api_key: str) -> list[Photo] | None:
    """For the page's hero background + featured card, not a day's gallery:
    the single most striking, sweeping SCENERY shots from the whole trip,
    ranked best-first — deliberately independent of any day's own picks."""
    instruction = (
        f"These are candidate photos from an entire trip. Choose the {n} most beautiful, "
        f"sweeping SCENERY / landscape shots - the kind that would work as a magazine "
        f"cover or a page's hero background image. Favour wide vistas, striking light, "
        f"dramatic nature or cityscapes. Avoid close-ups of food, documents, indoor detail "
        f"shots, or a photo where a person's face fills the frame. Order picks best first. "
        f"It's completely fine if a pick also belongs to (and will separately appear in) "
        f"its own day's gallery later on the page. Reply as JSON.")
    return _gemini_pick_raw(cands, n, model, api_key, instruction)


# ---------------------------------------------------------------------- output

def export(picks: list[Photo], key: str, images_dir: pathlib.Path, max_px: int,
           rel_prefix: str = "images/trips", preserve_order: bool = False) -> list[str]:
    images_dir.mkdir(parents=True, exist_ok=True)
    rel: list[str] = []
    ordered = picks if preserve_order else sorted(picks, key=lambda x: (x.taken or dt.datetime.min))
    for i, p in enumerate(ordered, start=1):
        dst = images_dir / f"{key}-{i}.jpg"
        with Image.open(p.path) as im:
            im = ImageOps.exif_transpose(im).convert("RGB")
            im.thumbnail((max_px, max_px))
            im.save(dst, "JPEG", quality=82, optimize=True)
        rel.append(f"{rel_prefix}/{dst.name}")
    return rel


# ------------------------------------------------------------------------- main

def select(photos: list[Photo], spec: dict, images_dir: pathlib.Path, *,
           per_region=3, minimum=2, candidates=12, dupe_distance=10, spread_distance=16,
           blur_min=0.0, max_px=1600, tz_offset=None, use_ai=True, model="gemini-3.6-flash",
           lodging_count=2, lodging_dir: pathlib.Path | None = None,
           dry_run=False, log=print) -> dict:
    """Bucket by exact calendar day -> score -> pick -> export. Writes each `day`
    item's `tripPhotos` in place (and `photos[key]['trip']` for `layover`s). On a
    check-in day (`isCheckIn`), also picks `lodging_count` more photos - from the
    same day's pool, excluding whatever was already picked for `tripPhotos` -
    into `photos[key]['lodging']`."""
    targets = load_targets(spec)
    if not targets:
        raise ValueError("לא נמצאו ימים/עצירות במסלול הטיול")
    tz_off = tz_offset if tz_offset is not None else float(spec.get("meta", {}).get("tz_offset_hours", 0))
    lodging_dir = lodging_dir if lodging_dir is not None else images_dir.parent / "lodging"

    analyse(photos)
    buckets = bucket(photos, targets, tz_off)
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    ai_on = use_ai and bool(api_key)

    plan: dict[str, list[str]] = {}
    lodging_plan: dict[str, list[str]] = {}
    for t in targets:
        pics = buckets.get(t.id, [])
        if not pics:
            log(f"{t.id}: no photos that day")
            continue
        good = dedupe([p for p in pics if p.sharp >= blur_min] or pics, dupe_distance)
        cands = sorted(good, key=lambda x: x.score, reverse=True)[:candidates]
        n = min(per_region, len(cands))
        if n < minimum:
            n = min(minimum, len(cands))
        chosen = None
        if ai_on and len(cands) > n:
            chosen = pick_with_gemini(cands, n, model, api_key, t.title)
        if not chosen:
            chosen = pick_diverse(cands, n, spread_distance)
        ordered = sorted(chosen, key=lambda x: (x.taken or dt.datetime.min))
        log(f"{t.id}: {len(pics)} that day -> pick {len(ordered)}"
            + ("  [gemini]" if ai_on and len(cands) > n and chosen else "  [offline]"))
        plan[t.id] = ([p.path.name for p in ordered] if dry_run
                      else export(ordered, t.id, images_dir, max_px))

        if t.kind == "day" and t.item.get("isCheckIn") and lodging_count > 0:
            used = {p.path for p in chosen}
            pool = sorted((p for p in good if p.path not in used),
                          key=lambda x: x.score, reverse=True)[:candidates]
            ln = min(lodging_count, len(pool))
            lchosen = None
            if ln and ai_on and len(pool) > ln:
                lchosen = pick_with_gemini(pool, ln, model, api_key,
                                           f"{t.title} - תמונות של הדירה/הבית עצמו, לא מהטיול")
            if ln and not lchosen:
                lchosen = pick_diverse(pool, ln, spread_distance)
            if lchosen:
                lordered = sorted(lchosen, key=lambda x: (x.taken or dt.datetime.min))
                log(f"{t.id}: + {len(lordered)} lodging photo(s)"
                    + ("  [gemini]" if ai_on and len(pool) > ln and lchosen else "  [offline]"))
                lodging_plan[t.id] = ([p.path.name for p in lordered] if dry_run
                                      else export(lordered, t.key, lodging_dir, max_px,
                                                  rel_prefix="images/lodging"))

    # the page's hero background + "featured" card: the most striking SCENERY
    # shots from the WHOLE trip (not bucketed by day), independent of any
    # single day's own picks - it's fine if a hero pick also appears again in
    # its own day's gallery below. Ranked best-first: [0] -> background, [1] -> card.
    hero_n = 2
    hero_pool = sorted(photos, key=lambda x: x.score, reverse=True)[:max(candidates, hero_n * 6)]
    hero_chosen = None
    if hero_pool:
        if ai_on and len(hero_pool) > hero_n:
            hero_chosen = pick_hero_with_gemini(hero_pool, hero_n, model, api_key)
        if not hero_chosen:
            hero_chosen = pick_diverse(hero_pool, hero_n, spread_distance)
    if hero_chosen:
        log(f"hero: picked {len(hero_chosen)} scenic photo(s) from the whole trip"
            + ("  [gemini]" if ai_on and len(hero_pool) > hero_n else "  [offline]"))

    if not dry_run:
        by_id = {t.id: t for t in targets}
        for tid, paths in plan.items():
            t = by_id[tid]
            if t.kind == "day":
                t.item["tripPhotos"] = paths
            else:
                spec.setdefault("photos", {}).setdefault(t.key, {"lodging": [], "trip": []})
                spec["photos"][t.key]["trip"] = paths
        for tid, paths in lodging_plan.items():
            t = by_id[tid]
            spec.setdefault("photos", {}).setdefault(t.key, {"lodging": [], "trip": []})
            spec["photos"][t.key]["lodging"] = paths
        if hero_chosen:
            spec.setdefault("hero", {})["photos"] = export(
                hero_chosen, "hero", images_dir, max_px, preserve_order=True)
    return spec


def load_photos(source: str, *, manifest=None, media_dir=None, folder=None) -> list[Photo]:
    if source == "folder":
        return load_from_folder(pathlib.Path(folder))
    return load_from_manifest(pathlib.Path(manifest), pathlib.Path(media_dir))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--spec", default="trip_spec.json")
    ap.add_argument("--source", choices=["picker", "folder"], default="picker")
    ap.add_argument("--manifest", default="gphotos/manifest.json")
    ap.add_argument("--media-dir", default="gphotos")
    ap.add_argument("--folder", default=None, help="photo folder for --source folder")
    ap.add_argument("--per-region", type=int, default=3, help="trip photos per day")
    ap.add_argument("--min", type=int, default=2, dest="minimum")
    ap.add_argument("--candidates", type=int, default=14)
    ap.add_argument("--lodging-count", type=int, default=2,
                    help="extra photos to pick for the lodging itself on check-in days")
    ap.add_argument("--lodging-dir", default="images/lodging")
    ap.add_argument("--dupe-distance", type=int, default=10, help="dHash Hamming <= this = duplicate")
    ap.add_argument("--spread-distance", type=int, default=16, help="dHash Hamming >= this = 'different enough'")
    ap.add_argument("--blur-min", type=float, default=0.0, help="drop frames below this Laplacian variance")
    ap.add_argument("--max-px", type=int, default=1600, help="long edge of exported jpg")
    ap.add_argument("--tz-offset", type=float, default=None, help="hours to add to photo UTC time before taking the date")
    ap.add_argument("--ai", dest="ai", action="store_true", default=True)
    ap.add_argument("--no-ai", dest="ai", action="store_false")
    ap.add_argument("--model", default="gemini-3.6-flash",
                    help="if this 404s, list options: py -c \"from google import genai,os; "
                         "[print(m.name) for m in genai.Client(api_key=os.environ['GEMINI_API_KEY']).models.list()]\"")
    ap.add_argument("--images-dir", default="images/trips")
    ap.add_argument("--clean-source", action="store_true", help="delete --media-dir/--folder after a successful run")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    root = pathlib.Path(__file__).resolve().parent
    rel = lambda x: pathlib.Path(x) if pathlib.Path(x).is_absolute() else root / x

    spec_path = rel(a.spec)
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    if a.source == "folder" and not a.folder:
        sys.exit("--source folder needs --folder <path>")
    photos = load_photos(a.source, manifest=rel(a.manifest), media_dir=rel(a.media_dir),
                         folder=rel(a.folder) if a.folder else None)
    if not photos:
        sys.exit("no photos loaded")
    print(f"loaded {len(photos)} photo(s)")

    select(photos, spec, rel(a.images_dir),
           per_region=a.per_region, minimum=a.minimum, candidates=a.candidates,
           dupe_distance=a.dupe_distance, spread_distance=a.spread_distance,
           blur_min=a.blur_min, max_px=a.max_px, tz_offset=a.tz_offset,
           use_ai=a.ai, model=a.model, lodging_count=a.lodging_count, lodging_dir=rel(a.lodging_dir),
           dry_run=a.dry_run, log=lambda m: print("  " + m))

    if a.dry_run:
        print("\n--dry-run: nothing written (picks logged above).")
        return 0

    spec_path.write_bytes((json.dumps(spec, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    total = (sum(len(it.get("tripPhotos", [])) for it in spec.get("timeline", []) if it.get("type") == "day")
             + sum(len(v.get("trip", [])) for v in spec.get("photos", {}).values()))
    lodging_total = sum(len(v.get("lodging", [])) for v in spec.get("photos", {}).values())
    hero_total = len(spec.get("hero", {}).get("photos") or [])
    print(f"\nwrote {total} trip + {lodging_total} lodging + {hero_total} hero photo(s) into "
          f"{a.images_dir}/ + {a.lodging_dir}/ and updated {a.spec}")

    if a.clean_source:
        import shutil
        src = rel(a.folder) if a.source == "folder" else rel(a.media_dir)
        shutil.rmtree(src, ignore_errors=True)
        print(f"removed source folder {src}")

    print("next:  python build_trip.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
