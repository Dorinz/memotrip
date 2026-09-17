# Trip journal generator

A `trip_spec.json` (the data) + `trip_template.html` (design/CSS/animation) →
finished HTML page. Two upstream pipelines fill the spec: photos (Phase 2) and
logistics docs (Phase 3). This repo is the **app/pipeline codebase** — no
single trip's data or output lives here; `webapp.py` generates and stores
each trip's spec/photos/page under `webapp_data/<trip-id>/`. (The original
Azores trip that seeded this whole project — its docs, photos, and the
one-off page they produced — was moved to `azores-archive/`, kept for
reference only; nothing in that folder is read by the app.)

## Files

| file | what it is |
|---|---|
| `trip_template.html` | **default** design: sticky sidebar identity + dark "itinerary row" day sections, adapted from a Figma community reference (see "Templates" below) |
| `trip_template_classic.html` | the original single-column coastal design (Azores hand-built page) — kept as a reference/fallback, not currently wired into the webapp |
| `build_trip.py` | fills the template from a trip's spec → its `page.html` |
| `parse_docs.py` | Phase 3 — logistics PDF/DOCX → a draft spec (+ `expand_days()`) |
| `gen_copy.py` | Phase 3 — spec + description → the Hebrew (or other) prose |
| `fetch_photos.py` / `select_photos.py` | Phase 2 — Google Photos → each day's gallery |
| `gemini_util.py` | shared Gemini call wrapper (retry/backoff) |
| `palette.py` | per-trip color theme generator |
| `local_env.py` | loads `.env` (the `GEMINI_API_KEY`) |
| `webapp.py` + `webapp_templates/` | the FastAPI app — this is the product |
| `webapp_data/` | **app-generated state**: one folder per trip (`spec.json`, `images/`, `docs/`, `page.html`) |
| `trips.db` | sqlite — trip list + pipeline status, used by `webapp.py` |
| `credentials.json` / `token.json` | Google Photos Picker OAuth (shared across trips) |
| `geocode_cache.json` | shared Nominatim lookup cache |

## Build (CLI, for dev/debugging outside the webapp)

```
python build_trip.py --spec some/trip_spec.json --out some/page.html
```

Keep `--out` in the same folder as that spec's `images/` so photo paths resolve.

## Verified

Phase 1 (template extraction): the initial `build_trip.py` output was diffed
against the original hand-built Azores page — CSS identical, all static
sections byte-identical, every rendered timeline section produced
byte-identical HTML.

Day-based restructuring (2026-09): an offline synthetic test (fake multi-day
spec + fake photos, no network calls) exercises `expand_days()` →
`select_photos.select()` → `build_trip.build()` end to end and asserts day
counts, `isCheckIn` placement, per-day photo counts, and the check-in-only
lodging pick. Then run for real against the Azores PDF/DOCX + a real
106-photo album (see `azores-archive/`): 14 real days extracted with real
host names and Airbnb codes, drama `from`/`to`/`fog` correctly backfilled,
all timeline items' location refs resolved, no missing image files, no
data-driven `undefined` in the output.

PDF text-extraction fix (2026-09-15): pypdf was shredding RTL PDF text to one
word per line, which a since-fixed "rejoin" regex was silently not undoing —
Gemini would lose structure/content near the end of longer documents (a real
test case lost the return flight and the last lodging entirely). Fixed by
flattening each page to one plain-text block instead of trying to preserve
line breaks; verified by re-extracting the same real document and confirming
the previously-missing transit + stay both appear.

## trip_spec.json shape

- `meta` — `title`, `tz_offset_hours`
- `hero` — `eyebrow`, `h1` (may contain `<em>`), `sub` (may contain `<br>`), `region`
  (2-4 words naming just the overall destination, e.g. "the Azores islands" —
  general, not an itinerary detail; shown on `trip_template.html`'s hero "featured"
  card), `photos[]` (0-2 paths — the pipeline's own pick of the most striking
  SCENERY shots from the *whole* trip, written by `select_photos.py`'s hero pass;
  `trip_template.html` uses `photos[0]` as the hero background and `photos[1]`
  in the featured card, falling back to day-1's own photo when absent, e.g. for
  specs built before this field existed), `meta[]` of `{n,l}`
- `outro` — `eyebrow`, `h2`, `p`, `stats[]` of `{n,l}`
- `economics` (optional) — `eyebrow`, `h2`, `intro`, `big[]` `{v,k}`, `bars[]` `{label,amt,width}`,
  `notes[]` `{k,html}`, `foot`. The automated pipeline never produces this (no cost
  data in logistics docs) — the whole section is dropped when it's absent; add it
  by hand if you want a cost breakdown.
- `locations` — `{key: {lat, lon, label}}`
- `photos` — `{stayKey: {lodging: [path], trip: [path]}}` — `lodging` only; a
  `day` item's own trip photos live on the item itself (`tripPhotos`, see below),
  `photos[key]["trip"]` is used only by `layover` items.
- `timeline[]` — ordered items. **One section per calendar day** (`type:"day"`),
  not per lodging — a multi-night stay becomes several `day` items sharing one
  `key`. Built by `parse_docs.assemble()` (still `type:"stay"`, one per
  lodging) then `parse_docs.expand_days()` (splits each `stay`'s `dateRange`
  into one `day` per date; `transit`/`drama`/`layover` pass through untouched):
  - `transit` — `mode` (`plane`/`ferry`), `from`, `to`, optional `viaStop`, `date`,
    optional `flightNo`, `stops[]`, `route`, optional `frame:[locKey,...]` to
    widen the map beyond `from`/`to`/`via`
  - `day` — `key` (shared across every day of the same stay), `dayIndex`,
    `dayCount`, `isCheckIn`, `date`, `city`, `island`, `dates` (short display
    label). **Only on the check-in day** (`isCheckIn:true`, filled from the
    source `stay` by `expand_days()`): `title` (small line above "City,
    Island"), `host`, optional `code`, optional `via` (booking channel —
    "Airbnb", "Booking.com", a hotel name...), optional `airbnb` (link),
    `about`, `stayDates`. On every day: `lede`, `tags[]`, and `tripPhotos[]`
    (that day's own 3 photos — written by `select_photos.select()`). On a
    non-check-in day `title` is the big heading instead of the small line.
  - `layover` — `key`, `city`, `title`, `dateRange`, `lede`, `tags[]`, optional `about`
    (a one-off stop that isn't split into days; its photos live in `photos[key]["trip"]`)
  - `drama` — `from`, `to`, `fog` (all location keys), `eyebrow`, `legs[]`,
    `paras[]`, `badge`. `assemble()` asks the model for these directly, and
    backfills `from`/`to`/`fog` from the surrounding transit/stay items when
    the model leaves them out (it often does — they overlap awkwardly with
    the free-text `note`).

## Phase 2 — photos

Two scripts turn a Google Photos album into the `trip` galleries.
`pip install -r requirements.txt` first.

### `fetch_photos.py` — album -> local folder

Needs a one-time Google Cloud setup (steps are in the file's header):
new project -> enable **Photos Picker API** -> OAuth consent screen (External,
add yourself as a Test user) -> OAuth client ID (**Desktop app**) -> save the
JSON as `credentials.json` next to the scripts.

```
python fetch_photos.py --out gphotos            # --size 1600 by default
```

Opens a Google page; you tick the trip photos (or the whole album) and hit Done.
Downloads a **downscaled copy** of each — `--size` px long edge, never the
original — into `gphotos/`, plus `gphotos/manifest.json`
(`downscale_px`, and per item: `id, file, createTime, orig_width, orig_height`).
The downscaled copies feed both the Gemini triage and the final page, so there's
no second download. Token cached in `token.json`. Picker `baseUrl`s expire with
the session, so run `select_photos.py` in the same sitting.

*Alternative with no cloud setup:* the album's ⋮ -> "Download all", unzip
somewhere, and use `select_photos.py --source folder` (dates come from EXIF).

### `select_photos.py` — choose + resize + wire in

```
# from a fetch
python select_photos.py --manifest gphotos/manifest.json --media-dir gphotos

# from a plain folder
python select_photos.py --source folder --folder ./album

# preview without touching anything
python select_photos.py --source folder --folder ./album --dry-run
```

What it does: one target per **calendar day** — a `day` item (exact `date`) or
a `layover` (its `dateRange[0]`); since the spec is already day-expanded, dates
never overlap, so each photo buckets to exactly one day. Scores sharpness/exposure,
drops near-duplicates (dHash), then picks `--per-region` (default 3, min
`--min` 2) that are sharp and visually varied for that day's `tripPhotos`. On a
check-in day it also picks `--lodging-count` (default 2) more from the same
day's pool (excluding whatever was just picked) into `photos[key]["lodging"]`.
With `GEMINI_API_KEY` in the environment it asks `--model` (default
`gemini-3.6-flash`) to make each pick from the candidates; otherwise (or if the
call fails, e.g. a quota 429) a deterministic quality+diversity picker.
Chosen photos are re-saved (`--max-px` 1600, JPEG q82, metadata stripped) into
`images/trips/<dayId>-N.jpg` (dayId like `capelas-d2`) and `images/lodging/<key>-N.jpg`,
written onto the `day` item's `tripPhotos` (or `photos[key]["trip"]` for a
`layover`) and `photos[key]["lodging"]`.

Separately (once per run, not per-day): also picks the **2 best scenery shots
from the whole trip** — the same quality-scored pool as above, but judged for
"sweeping landscape/cityscape, hero-worthy" rather than "varied set for this
day", and completely independent of any day's own picks (duplication is fine —
a hero pick still shows up again in its own day's gallery). Written to
`hero.photos[]` (best-first) and exported to `images/trips/hero-N.jpg`.
`trip_template.html` uses these for the page's hero background + featured card.

Then run `build_trip.py`.

Knobs: `--per-region --min --candidates --dupe-distance --spread-distance
--blur-min --max-px --tz-offset --no-ai --lodging-count --lodging-dir
--images-dir --clean-source --dry-run`. `--clean-source` deletes the media
folder after a successful run. `tz-offset` (hours added to the photo's UTC
time before taking the date) defaults to `meta.tz_offset_hours` in the spec.

Needs in the spec: it must already be day-expanded (`parse_docs.assemble()`
followed by `parse_docs.expand_days()` — the CLI and `run_build()` in
`webapp.py` both do this automatically) and `meta.tz_offset_hours`.

## Phase 3 — logistics docs → draft spec

`parse_docs.py` reads the trip's PDF/DOCX logistics notes and writes
`trip_spec.draft.json` (timeline + meta + geocoded locations, no prose). It
never touches a good `trip_spec.json` — you review and merge by hand.

```
python parse_docs.py                              # every *.pdf/*.docx here; AI extract if GEMINI_API_KEY set
python parse_docs.py --no-ai --no-geocode         # regex-only, offline (DOCX sections)
python parse_docs.py --docs trip.docx --geocode-hint "Iceland"
```

- **AI path** (`gemini-3.6-flash`): normalizes place names, catches the
  disruption/"drama" item, produces clean `dateRange`s and `key`s. Also calls
  `expand_days()` before writing, so the output is already one `day` item per
  calendar date (not one per lodging).
- **`--no-ai`**: regex over the DOCX's `✈️ / 🚢 / 🏨` sections. Best-effort —
  place names come out inconsistent (airport codes vs Hebrew vs port names), no
  drama item; still gets day-expanded.
- Geocoding: OpenStreetMap Nominatim, cached in `geocode_cache.json`, 1 req/sec.
  Unresolved places are listed; fill their lat/lon by hand.

### `gen_copy.py` — write the prose

Fills the Hebrew text of a spec with Gemini (needs `GEMINI_API_KEY`; no offline
mode — you can't regex evocative travel writing). Two API calls: one trip-level
(`meta.title`, `hero`, `outro`, the `drama` story), one for **every day**
(`title`, `island`, `lede`, `tags`, plus `about` only on check-in days) —
matched back by `(key, dayIndex)` since `key` repeats across a stay's days.

```
python gen_copy.py --description trip.txt --docs "*.pdf"          # -> trip_spec.copy.json
python gen_copy.py --spec trip_spec.draft.json --description "היינו שבועיים..." --overwrite --in-place
python gen_copy.py --description trip.txt --dry-run               # show the prompts, call nothing
```

By default only fills empty fields and writes to `trip_spec.copy.json` for
review; `--overwrite` regenerates text that's already there, `--in-place` edits
the spec directly. Feed `--docs` so `tags`/`about` are grounded in the real
per-day activity notes.

## Full pipeline (a new trip, start to finish)

```
python parse_docs.py                                  # logistics docs -> trip_spec.draft.json (already day-expanded)
# review / fix place names + missing coords, save as trip_spec.json
python gen_copy.py --spec trip_spec.json --description trip.txt --docs "*.pdf" --in-place
python fetch_photos.py --out gphotos                  # pick photos in the browser
python select_photos.py --manifest gphotos/manifest.json --media-dir gphotos
python build_trip.py                                  # -> the page
```

## Color theme

Every trip gets its own palette, generated once (`palette.py`, pure color
theory, no external service) from **three deliberately distinct hue families**
rather than one hue at different shades — so the result is randomized but never
monochrome:
- `bg_hue` — always a warm cream/parchment range (28-48°), so every trip reads
  as the same kind of warm travel journal regardless of the accent;
- `accent_hue` — the vivid "turquoise" accent (links, map marks, the sidebar
  block), rotated across a wide cool-to-vivid arc (140-320°) that's kept clear
  of the cream range so it always pops against the paper;
- `ink_hue` — offset far from the accent (70-140°, e.g. turquoise accent +
  navy ink), used for text, the "sea" secondary accent, and the dark
  "itinerary row" day-sections — so a panel like the sidebar shows real
  bg/text hue contrast instead of one hue at two lightnesses.

A contrast pass (WCAG-ratio checked against the actual background each color
sits on) guarantees legible pairs regardless of which hues come out. Stored in
`trip_spec.json`'s `theme` block; rebuilding a trip (e.g. after adding photos)
re-reads the same stored theme, so colors never shift between rebuilds. A spec
with no `theme` renders with `palette.DEFAULT` — the Azores page's original
hand-tuned colors (so it keeps looking the same).

```
python palette.py [seed]     # print a sample palette + its contrast ratios
```

`webapp.py` assigns the theme automatically per trip (seeded by the trip id).
Using `build_trip.py`/`parse_docs.py` by hand: set `spec["theme"] =
palette.generate_palette()` yourself before calling `build()`.

## Phase 4 — local web app (MVP)

```
pip install -r requirements.txt
python webapp.py                # -> http://localhost:8000
```

The Gemini key: put it once in a `.env` file next to the scripts —
`GEMINI_API_KEY=...` — and every script + the app loads it automatically
(`local_env.py`, gitignored). No need to re-set a shell variable each session.
A real `GOOGLE_API_KEY`/`GEMINI_API_KEY` already in the environment still wins.

Paste a description + upload logistics docs → the pipeline runs in a background
thread (parse_docs.assemble → expand_days → geocode → gen_copy → build_trip) →
preview / download the page → "Add photos" runs the Google Photos Picker
(reuses `credentials.json` / `token.json`) then select_photos + rebuild.
State: `trips.db` (sqlite) + `webapp_data/<id>/`. Single user, localhost, no
app auth — that's the MVP.

Without `GEMINI_API_KEY` it still builds a page, but thin: regex-only doc parsing
(rough place keys, no drama detection) and no generated prose. With the key the
`parse_ai` + `gen_copy` paths produce a real draft.

The 5 pipeline scripts each expose a callable now (`build_trip.build`,
`parse_docs.assemble`, `gen_copy.generate_copy`, `select_photos.select` /
`load_photos`, `fetch_photos.open_session` / `session_ready` / `collect`); the
CLIs are thin wrappers over them. `build_trip.build` tolerates a spec missing
`hero`/`outro`/`economics` (drops the economics section when there's no data).

## Notes / not done

- UI micro-copy ("קצת רקע", "תמונות מהדירה", …) is still hard-coded in the template;
  a `strings` block in the spec would make it language-agnostic.
- Map framing is now data-driven: `buildMap` frames each leg on its `from`/`to` +
  optional `via` + an optional `frame: [locKeys]` on the transit item (the Azores
  triangle ferries use `frame` to show all four ports). `GROUPS`/`groupFor` are gone.
