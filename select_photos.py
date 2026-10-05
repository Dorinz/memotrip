#!/usr/bin/env python3
"""
select_photos.py  —  Phase 2, step 2 of the trip-journal generator.

Takes a folder of trip photos (from fetch_photos.py, or a manual album download),
buckets them into the trip's calendar days by *local* date at the place we were
that day, throws out the blurry / near-duplicate ones, then picks the best few
per day -- with Gemini if a key is available, or a deterministic quality+diversity
fallback otherwise. Chosen photos are resized into images/trips/<key>-N.jpg and
wired into trip_spec.json. Lodging photos never come from the album.

Run  build_trip.py  afterwards to regenerate the page.

Examples
--------
  # photos fetched by fetch_photos.py
  python select_photos.py --manifest gphotos/manifest.json --media-dir gphotos

  # a plain folder (e.g. Google Photos "Download all"); dates read from EXIF
  python select_photos.py --source folder --folder "C:/Users/me/Downloads/azores"

  # see the plan without writing anything
  python select_photos.py --source folder --folder ./album --dry-run

Needs: Pillow (always). timezonefinder for per-day local dates (falls back to
UTC without it). google-genai only for the --ai path.
"""

from __future__ import annotations

try:
    import local_env  # noqa: F401  (loads .env)
except Exception:
    pass

import argparse
import datetime as dt
import io
import json
import os
import pathlib
import sys
from dataclasses import dataclass
from zoneinfo import ZoneInfo

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
EXIF_OFFSET_TIME_ORIGINAL = 36881
EXIF_OFFSET_TIME = 36880
EXIF_IFD = 0x8769
# a photo outside every day's window by at most this much (a midnight shot on a
# day we crossed time zones) still goes to the nearest day instead of being dropped
TZ_GAP_TOLERANCE = dt.timedelta(hours=3)
# long edge of the photos shown on the page; analysis needs far less (it works
# on 512px), so the webapp downloads everything small and only candidates at this
PAGE_PX = 1600


# --------------------------------------------------------------------------- data


@dataclass
class Photo:
    path: pathlib.Path
    taken: dt.datetime | None  # naive UTC, or naive local wall-clock when `local`
    width: int = 0
    height: int = 0
    # True when `taken` is the camera's local wall-clock with no known offset
    # (EXIF DateTimeOriginal without OffsetTimeOriginal): its date already is the
    # local date, so it must not be shifted by any time zone
    local: bool = False
    # the page-size copy exported for the page; `path` itself may be a small
    # (512px) analysis copy. None = not fetched yet (see select()'s fetch_full)
    full_path: pathlib.Path | None = None
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

    id: str  # output id, e.g. "capelas-d2" or "lisbon_layover"
    date: dt.date
    title: str = ""
    kind: str = "day"  # "day" | "layover"
    item: dict | None = None  # the `day` item to write .tripPhotos back onto
    key: str = ""  # for "layover": which spec["photos"][key]["trip"] to fill
    tz: str | None = None  # IANA zone of the place that day, e.g. "Atlantic/Azores"


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


def _exif_datetime(img: Image.Image) -> tuple[dt.datetime | None, bool]:
    """(taken, local): naive UTC when the camera also recorded its UTC offset,
    else the naive local wall-clock with local=True. (None, False) if absent."""
    try:
        ex = img.getexif()
    except Exception:
        return None, False
    try:
        sub = ex.get_ifd(EXIF_IFD)
    except Exception:
        sub = {}
    for tag, off_tag in (
        (EXIF_DATETIME_ORIGINAL, EXIF_OFFSET_TIME_ORIGINAL),
        (EXIF_DATETIME, EXIF_OFFSET_TIME),
    ):
        v = sub.get(tag) or ex.get(tag)
        if not (isinstance(v, str) and len(v) >= 19):
            continue
        try:
            taken = dt.datetime.strptime(v[:19], "%Y:%m:%d %H:%M:%S")
        except ValueError:
            continue
        off = sub.get(off_tag) or ex.get(off_tag)
        if isinstance(off, str):
            try:
                aware = dt.datetime.fromisoformat(taken.isoformat() + off.strip())
                return aware.astimezone(dt.timezone.utc).replace(tzinfo=None), False
            except ValueError:
                pass
        return taken, True
    return None, False


def load_from_manifest(manifest: pathlib.Path, media_dir: pathlib.Path) -> list[Photo]:
    """`file` is the analysis copy. If it was downloaded at page size already
    (downscale_px >= PAGE_PX, e.g. the CLI or trips made before the two-size
    download) it doubles as the page copy; otherwise the page copy is
    `full_file` once upgrade() has fetched it."""
    data = json.loads(manifest.read_text(encoding="utf-8"))
    page_size = int(data.get("downscale_px") or 0) >= PAGE_PX
    out: list[Photo] = []
    for it in data.get("items", []):
        p = pathlib.Path(it.get("file") or "")
        if not p.is_absolute():
            p = media_dir / p.name
        if not p.exists():
            print(f"  ! missing file for manifest item: {p}", file=sys.stderr)
            continue
        full = media_dir / it["full_file"] if it.get("full_file") else None
        # dims are read from the file in analyse(); orig_* (pre-downscale) drives the
        # resolution bonus so a downscaled 12MP shot still outranks a downscaled phone crop
        out.append(
            Photo(
                path=p,
                taken=_parse_iso(it.get("createTime", "")),
                width=int(it.get("orig_width") or it.get("width") or 0),
                height=int(it.get("orig_height") or it.get("height") or 0),
                full_path=p if page_size else (full if full and full.exists() else None),
            )
        )
    return out


def load_from_folder(folder: pathlib.Path) -> list[Photo]:
    out: list[Photo] = []
    for p in sorted(folder.rglob("*")):
        if p.suffix.lower() not in IMG_EXTS or not p.is_file():
            continue
        try:
            with Image.open(p) as im:
                w, h = im.size
                taken, local = _exif_datetime(im)
        except Exception as e:
            print(f"  ! cannot read {p.name}: {e}", file=sys.stderr)
            continue
        # no file-mtime fallback: a download or copy resets it, which would put
        # the photo on the wrong day. No EXIF time -> bucket() reports it unmatched.
        out.append(Photo(path=p, taken=taken, width=w, height=h, local=local, full_path=p))
    return out


_TF = None


def tz_at(lat, lon) -> str | None:
    """IANA time zone at a coordinate (offline lookup), or None."""
    global _TF
    if lat is None or lon is None:
        return None
    try:
        if _TF is None:
            from timezonefinder import TimezoneFinder

            _TF = TimezoneFinder()
        return _TF.timezone_at(lat=float(lat), lng=float(lon))
    except Exception:
        return None


def _place_tz(item: dict, spec: dict) -> str | None:
    """Zone of the place a day/layover happens in: its city (else island) looked
    up by label in spec["locations"], then coordinate -> zone."""
    by_label = {
        (v.get("label") or "").strip().lower(): v for v in (spec.get("locations") or {}).values()
    }
    for name in (item.get("city"), item.get("island")):
        loc = by_label.get((name or "").strip().lower())
        if loc:
            z = tz_at(loc.get("lat"), loc.get("lon"))
            if z:
                return z
    return None


def load_targets(spec: dict) -> list[Target]:
    """One target per `day` item (by its exact date) and per `layover` (by
    dateRange[0]). Since the spec is already day-expanded, dates don't overlap.
    Each target gets its place's time zone; one whose place can't be resolved
    borrows the zone of the nearest earlier (else later) target."""
    targets: list[Target] = []
    for item in spec.get("timeline", []):
        if item.get("type") == "day" and item.get("date"):
            targets.append(
                Target(
                    id=f'{item["key"]}-d{item["dayIndex"]}',
                    date=dt.date.fromisoformat(item["date"]),
                    title=item.get("title") or item.get("city") or item["key"],
                    kind="day",
                    item=item,
                    key=item["key"],
                    tz=_place_tz(item, spec),
                )
            )
        elif item.get("type") == "layover":
            rng = item.get("dateRange")
            if isinstance(rng, list) and rng and rng[0]:
                targets.append(
                    Target(
                        id=item["key"],
                        date=dt.date.fromisoformat(rng[0]),
                        title=item.get("title") or item.get("city") or item["key"],
                        kind="layover",
                        key=item["key"],
                        tz=_place_tz(item, spec),
                    )
                )
            else:
                print(f"  ! {item.get('key')} has no dateRange - skipped", file=sys.stderr)
    prev = None
    for t in targets:
        t.tz = prev = t.tz or prev
    nxt = None
    for t in reversed(targets):
        t.tz = nxt = t.tz or nxt
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
    flat = max(0.0, (28.0 - stdev) / 28.0)  # low-contrast frames
    return min(1.5, crushed * 2.2 + blown * 2.2 + flat)


def dhash(gray9x8: Image.Image) -> int:
    g = gray9x8.resize((9, 8)).convert("L")
    px = g.tobytes()  # 72 bytes, one grey level per pixel
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


def day_window_utc(t: Target) -> tuple[dt.datetime, dt.datetime]:
    """[local midnight, next local midnight) of the target's date at the target's
    own place, as naive UTC. Unknown zone -> the UTC day."""
    z = ZoneInfo(t.tz) if t.tz else dt.timezone.utc
    start, end = (
        dt.datetime.combine(d, dt.time(), z).astimezone(dt.timezone.utc).replace(tzinfo=None)
        for d in (t.date, t.date + dt.timedelta(days=1))
    )
    return start, end


def bucket(photos: list[Photo], targets: list[Target]) -> dict[str, list[Photo]]:
    """A photo belongs to the day whose local calendar date, at that day's place,
    contains the moment it was taken - each day has its own zone, so a Lisbon
    evening and an Azores morning on a multi-zone trip both land right."""
    by_date: dict[dt.date, Target] = {t.date: t for t in targets}  # day-expansion -> no overlaps
    by_id: dict[str, list[Photo]] = {t.id: [] for t in targets}
    windows = [(t, *day_window_utc(t)) for t in targets]
    unmatched = 0
    for p in photos:
        if p.taken is None:
            unmatched += 1
            continue
        if p.local:  # camera wall-clock: its date already is the local date
            hit = by_date.get(p.taken.date())
        else:
            hit = next((t for t, a, b in windows if a <= p.taken < b), None)
            if hit is None and windows:  # in the seam where we changed zones
                gap, near = min(
                    ((max(a - p.taken, p.taken - b), t) for t, a, b in windows),
                    key=lambda x: x[0],
                )
                hit = near if gap <= TZ_GAP_TOLERANCE else None
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
    if len(picks) < n:  # loosen if we came up short
        for p in pool:
            if len(picks) >= n:
                break
            if p not in picks:
                picks.append(p)
    return picks[:n]


def thumbnail_bytes(p: Photo, box: int = 512) -> bytes:
    """The JPEG thumbnail Gemini sees for one candidate."""
    with Image.open(p.path) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        im.thumbnail((box, box))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=80)
    return buf.getvalue()


def candidate_parts(cands: list[Photo], label=lambda i: f"[{i}]") -> list:
    """Each candidate as a text label followed by its thumbnail."""
    from google.genai import types

    parts = []
    for i, p in enumerate(cands):
        parts.append(types.Part.from_text(text=label(i)))
        parts.append(types.Part.from_bytes(data=thumbnail_bytes(p), mime_type="image/jpeg"))
    return parts


def _gemini_pick_raw(
    cands: list[Photo], n: int, model: str, api_key: str, instruction: str
) -> list[Photo] | None:
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
        parts = [types.Part.from_text(text=instruction)] + candidate_parts(cands)
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
        data = gu.generate_json(
            model, [types.Content(role="user", parts=parts)], schema=schema, temperature=0.4, cl=cl
        )
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


def pick_with_gemini(
    cands: list[Photo], n: int, model: str, api_key: str, stay_title: str
) -> list[Photo] | None:
    instruction = (
        f"These are candidate photos for one leg of a trip ('{stay_title}'). "
        f"Choose the {n} best to show together. Favour sharp, well-exposed, "
        f"interesting frames, and make the set varied - different scenes, "
        f"subjects and moments, never near-duplicates. Reply as JSON."
    )
    return _gemini_pick_raw(cands, n, model, api_key, instruction)


def pick_hero_with_gemini(
    cands: list[Photo], n: int, model: str, api_key: str
) -> list[Photo] | None:
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
        f"its own day's gallery later on the page. Reply as JSON."
    )
    return _gemini_pick_raw(cands, n, model, api_key, instruction)


# ---------------------------------------------------------------------- output


def export(
    picks: list[Photo],
    key: str,
    images_dir: pathlib.Path,
    max_px: int,
    rel_prefix: str = "images/trips",
    preserve_order: bool = False,
) -> list[str]:
    images_dir.mkdir(parents=True, exist_ok=True)
    rel: list[str] = []
    ordered = picks if preserve_order else sorted(picks, key=lambda x: (x.taken or dt.datetime.min))
    for i, p in enumerate(ordered, start=1):
        dst = images_dir / f"{key}-{i}.jpg"
        with Image.open(p.full_path or p.path) as im:
            im = ImageOps.exif_transpose(im).convert("RGB")
            im.thumbnail((max_px, max_px))
            im.save(dst, "JPEG", quality=82, optimize=True)
        rel.append(f"{rel_prefix}/{dst.name}")
    return rel


# ------------------------------------------------------------------------- main


def day_candidates(
    pics: list[Photo],
    *,
    per_region=3,
    minimum=2,
    candidates=12,
    dupe_distance=10,
    blur_min=0.0,
) -> tuple[list[Photo], int]:
    """A day's shortlist (deduped, best-scored first, capped at `candidates`)
    and how many of them to pick."""
    good = dedupe([p for p in pics if p.sharp >= blur_min] or pics, dupe_distance)
    cands = sorted(good, key=lambda x: x.score, reverse=True)[:candidates]
    n = min(per_region, len(cands))
    if n < minimum:
        n = min(minimum, len(cands))
    return cands, n


def hero_pool(photos: list[Photo], candidates=12, hero_n=2) -> list[Photo]:
    """Shortlist for the hero: the best-scored photos of the whole trip."""
    return sorted(photos, key=lambda x: x.score, reverse=True)[: max(candidates, hero_n * 6)]


def select(
    photos: list[Photo],
    spec: dict,
    images_dir: pathlib.Path,
    *,
    per_region=3,
    minimum=2,
    candidates=12,
    dupe_distance=10,
    spread_distance=16,
    blur_min=0.0,
    max_px=1600,
    use_ai=True,
    model="gemini-3.6-flash",
    fetch_full=None,
    dry_run=False,
    log=print,
) -> dict:
    """Bucket by local calendar day -> score -> shortlist -> pick -> export.
    Writes each `day` item's `tripPhotos` in place (and `photos[key]['trip']` for
    `layover`s), plus the hero. Lodging photos are never taken from the album.

    `fetch_full(photos)`, if given, must set `full_path` on each photo it can (the
    page-size copy); it is called once, for every shortlisted photo still missing
    one, before anything is picked."""
    targets = load_targets(spec)
    if not targets:
        raise ValueError("לא נמצאו ימים/עצירות במסלול הטיול")
    if not any(t.tz for t in targets):
        log("note: no time zone found for any day's place - bucketing photos by UTC date")

    analyse(photos)
    buckets = bucket(photos, targets)
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    ai_on = use_ai and bool(api_key)

    # pass 1: every day's shortlist and the hero's, before anything is picked
    shortlists = []
    for t in targets:
        pics = buckets.get(t.id, [])
        if not pics:
            log(f"{t.id}: no photos that day")
            continue
        cands, n = day_candidates(
            pics,
            per_region=per_region,
            minimum=minimum,
            candidates=candidates,
            dupe_distance=dupe_distance,
            blur_min=blur_min,
        )
        shortlists.append((t, pics, cands, n))
    hero_n = 2
    pool = hero_pool(photos, candidates, hero_n)

    # page-size copies for the whole shortlist in one batch - not just the final
    # picks - so a later rerun (same small files -> same shortlists, even if Gemini
    # then picks differently) never needs a copy it can no longer download
    day_cands = [p for _, _, cands, _ in shortlists for p in cands]
    shortlisted = list({p.path: p for p in day_cands + pool}.values())
    if not dry_run:
        need = [p for p in shortlisted if p.full_path is None]
        if need and fetch_full:
            log(f"fetching page-size copies of {len(need)} shortlisted photo(s)")
            fetch_full(need)
        missing = sum(p.full_path is None for p in shortlisted)
        if missing:
            log(f"note: {missing} shortlisted photo(s) have no page-size copy - low-res if picked")

    # pass 2: pick and export
    plan: dict[str, list[str]] = {}
    for t, pics, cands, n in shortlists:
        chosen = None
        if ai_on and len(cands) > n:
            chosen = pick_with_gemini(cands, n, model, api_key, t.title)
        if not chosen:
            chosen = pick_diverse(cands, n, spread_distance)
        ordered = sorted(chosen, key=lambda x: (x.taken or dt.datetime.min))
        log(
            f"{t.id}: {len(pics)} that day -> pick {len(ordered)}"
            + ("  [gemini]" if ai_on and len(cands) > n and chosen else "  [offline]")
        )
        plan[t.id] = (
            [p.path.name for p in ordered] if dry_run else export(ordered, t.id, images_dir, max_px)
        )

    # the page's hero background + "featured" card: the most striking SCENERY
    # shots from the WHOLE trip (not bucketed by day), independent of any
    # single day's own picks - it's fine if a hero pick also appears again in
    # its own day's gallery below. Ranked best-first: [0] -> background, [1] -> card.
    hero_chosen = None
    if pool:
        if ai_on and len(pool) > hero_n:
            hero_chosen = pick_hero_with_gemini(pool, hero_n, model, api_key)
        if not hero_chosen:
            hero_chosen = pick_diverse(pool, hero_n, spread_distance)
    if hero_chosen:
        log(
            f"hero: picked {len(hero_chosen)} scenic photo(s) from the whole trip"
            + ("  [gemini]" if ai_on and len(pool) > hero_n else "  [offline]")
        )

    if not dry_run:
        by_id = {t.id: t for t in targets}
        for tid, paths in plan.items():
            t = by_id[tid]
            if t.kind == "day":
                t.item["tripPhotos"] = paths
            else:
                spec.setdefault("photos", {}).setdefault(t.key, {"lodging": [], "trip": []})
                spec["photos"][t.key]["trip"] = paths
        if hero_chosen:
            spec.setdefault("hero", {})["photos"] = export(
                hero_chosen, "hero", images_dir, max_px, preserve_order=True
            )
    return spec


def load_photos(source: str, *, manifest=None, media_dir=None, folder=None) -> list[Photo]:
    if source == "folder":
        return load_from_folder(pathlib.Path(folder))
    return load_from_manifest(pathlib.Path(manifest), pathlib.Path(media_dir))


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--spec", default="trip_spec.json")
    ap.add_argument("--source", choices=["picker", "folder"], default="picker")
    ap.add_argument("--manifest", default="gphotos/manifest.json")
    ap.add_argument("--media-dir", default="gphotos")
    ap.add_argument("--folder", default=None, help="photo folder for --source folder")
    ap.add_argument("--per-region", type=int, default=3, help="trip photos per day")
    ap.add_argument("--min", type=int, default=2, dest="minimum")
    ap.add_argument("--candidates", type=int, default=14)
    ap.add_argument(
        "--dupe-distance", type=int, default=10, help="dHash Hamming <= this = duplicate"
    )
    ap.add_argument(
        "--spread-distance", type=int, default=16, help="dHash Hamming >= this = 'different enough'"
    )
    ap.add_argument(
        "--blur-min", type=float, default=0.0, help="drop frames below this Laplacian variance"
    )
    ap.add_argument("--max-px", type=int, default=1600, help="long edge of exported jpg")
    ap.add_argument("--ai", dest="ai", action="store_true", default=True)
    ap.add_argument("--no-ai", dest="ai", action="store_false")
    ap.add_argument(
        "--model",
        default="gemini-3.6-flash",
        help='if this 404s, list options: py -c "from google import genai,os; '
        "[print(m.name) for m in "
        "genai.Client(api_key=os.environ['GEMINI_API_KEY']).models.list()]\"",
    )
    ap.add_argument("--images-dir", default="images/trips")
    ap.add_argument(
        "--clean-source",
        action="store_true",
        help="delete --media-dir/--folder after a successful run",
    )
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    root = pathlib.Path(__file__).resolve().parent

    def rel(x):
        return pathlib.Path(x) if pathlib.Path(x).is_absolute() else root / x

    spec_path = rel(a.spec)
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    if a.source == "folder" and not a.folder:
        sys.exit("--source folder needs --folder <path>")
    photos = load_photos(
        a.source,
        manifest=rel(a.manifest),
        media_dir=rel(a.media_dir),
        folder=rel(a.folder) if a.folder else None,
    )
    if not photos:
        sys.exit("no photos loaded")
    print(f"loaded {len(photos)} photo(s)")

    select(
        photos,
        spec,
        rel(a.images_dir),
        per_region=a.per_region,
        minimum=a.minimum,
        candidates=a.candidates,
        dupe_distance=a.dupe_distance,
        spread_distance=a.spread_distance,
        blur_min=a.blur_min,
        max_px=a.max_px,
        use_ai=a.ai,
        model=a.model,
        dry_run=a.dry_run,
        log=lambda m: print("  " + m),
    )

    if a.dry_run:
        print("\n--dry-run: nothing written (picks logged above).")
        return 0

    spec_path.write_bytes((json.dumps(spec, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    total = sum(
        len(it.get("tripPhotos", [])) for it in spec.get("timeline", []) if it.get("type") == "day"
    ) + sum(len(v.get("trip", [])) for v in spec.get("photos", {}).values())
    hero_total = len(spec.get("hero", {}).get("photos") or [])
    print(
        f"\nwrote {total} trip + {hero_total} hero photo(s) into "
        f"{a.images_dir}/ and updated {a.spec}"
    )

    if a.clean_source:
        import shutil

        src = rel(a.folder) if a.source == "folder" else rel(a.media_dir)
        shutil.rmtree(src, ignore_errors=True)
        print(f"removed source folder {src}")

    print("next:  python build_trip.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
