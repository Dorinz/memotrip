#!/usr/bin/env python3
"""
parse_docs.py  —  Phase 3, step 1 of the trip-journal generator.

Reads the logistics documents (PDF / DOCX) and produces a *draft* trip_spec:
the `timeline` (flights, ferries, stays, the disruption), `meta`, and geocoded
`locations`.  It writes  trip_spec.draft.json  — it never touches a good
trip_spec.json.  Review / merge by hand, then gen_copy.py fills the prose.

    pip install -r requirements.txt
    python parse_docs.py --docs "*.pdf" "*.docx"          # AI extraction (needs GEMINI_API_KEY)
    python parse_docs.py --no-ai --docs logistics.docx    # regex fallback (DOCX sections only)
    python parse_docs.py --no-geocode ...                 # skip lat/lon lookup

Text extraction: pypdf + python-docx.  Geocoding: OpenStreetMap Nominatim
(cached in geocode_cache.json; be gentle, 1 req/sec).
"""

from __future__ import annotations

try:
    import local_env  # noqa: F401  (loads .env)
except Exception:
    pass

import argparse
import glob
import json
import pathlib
import re
import sys
import time
from datetime import date, timedelta

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

DRAFT = "trip_spec.draft.json"
GEO_CACHE = "geocode_cache.json"
NOMINATIM = "https://nominatim.openstreetmap.org/search"
UA = "trip-journal-generator/0.1 (personal project)"

IATA = {  # airport code -> (place name, geocode hint)
    "TLV": "תל אביב",
    "LIS": "Lisboa",
    "PDL": "Ponta Delgada",
    "PIX": "Pico",
    "HOR": "Horta",
    "SJZ": "São Jorge",
    "TER": "Terceira",
}
HEB_MONTHS = {
    "ינואר": 1,
    "פברואר": 2,
    "מרץ": 3,
    "אפריל": 4,
    "מאי": 5,
    "יוני": 6,
    "יולי": 7,
    "אוגוסט": 8,
    "ספטמבר": 9,
    "אוקטובר": 10,
    "נובמבר": 11,
    "דצמבר": 12,
}


# --------------------------------------------------------------------- text extraction


def read_doc(path: pathlib.Path) -> str:
    ext = path.suffix.lower()
    if ext == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            sys.exit("pip install pypdf")
        # pypdf on RTL PDFs shreds text into one word (or even one lone space) per
        # extracted line - e.g. "word1\n \nword2\n \n..." - with no reliable signal
        # left for real paragraph breaks (a blank line is just as likely to be an
        # inter-word space as an actual break). A lookahead-based rejoin can't fix
        # this (tried - it only ever rejoins every other line); flatten each page's
        # words into one readable block instead. Day/list markers ("יום 1 -", "●",
        # "1 .") survive as plain tokens, so the model can still find structure.
        out = []
        for p in PdfReader(str(path)).pages:
            words = [w.strip() for w in (p.extract_text() or "").split("\n")]
            out.append(" ".join(w for w in words if w))
        return re.sub(r"[ \t]+", " ", "\n\n".join(out))
    if ext in (".docx", ".doc"):
        try:
            import docx
        except ImportError:
            sys.exit("pip install python-docx")
        d = docx.Document(str(path))
        out = [p.text for p in d.paragraphs if p.text.strip()]
        for tbl in d.tables:
            for row in tbl.rows:
                cells = [c.text.strip().replace("\n", " ") for c in row.cells]
                if any(cells):
                    out.append(" | ".join(cells))
        return "\n".join(out)
    return path.read_text(encoding="utf-8", errors="replace")


# ------------------------------------------------------------------------ dates


def heb_date(s: str, default_year: int | None = None):
    """'3 באוגוסט 2026' or '12/8' or '12.8' -> 'YYYY-MM-DD' (year guessed if absent)."""
    s = s.strip()
    m = re.search(r"(\d{1,2})\s+ב?(" + "|".join(HEB_MONTHS) + r")(?:\s+(\d{4}))?", s)
    if m:
        day, mon = int(m.group(1)), HEB_MONTHS[m.group(2)]
        year = int(m.group(3)) if m.group(3) else (default_year or time.localtime().tm_year)
        return f"{year:04d}-{mon:02d}-{day:02d}"
    m = re.search(r"(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?", s)
    if m:
        day, mon = int(m.group(1)), int(m.group(2))
        year = int(m.group(3)) if m.group(3) else (default_year or time.localtime().tm_year)
        if year < 100:
            year += 2000
        return f"{year:04d}-{mon:02d}-{day:02d}"
    return None


# ------------------------------------------------------------ regex fallback (DOCX)


def parse_docx_regex(text: str) -> dict:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    year = None
    for ln in lines:
        m = re.search(r"\b(20\d{2})\b", ln)
        if m:
            year = int(m.group(1))
            break

    flights, ferries, stays = [], [], []

    for i, ln in enumerate(lines):
        # flights:  "טיסה TP8920: ... (TLV) ... (LIS) ... 17:10 ... 21:10"
        fm = re.search(r"טיסה\s+([A-Z]{2}\d{2,4})\b", ln)
        if fm:
            codes = re.findall(r"\(([A-Z]{3})\)", ln)
            times = re.findall(r"\b(\d{1,2}:\d{2})\b", ln)
            hdr = lines[i - 1] if i else ""
            d = heb_date(ln) or heb_date(hdr, year)
            flights.append(
                {
                    "flightNo": fm.group(1),
                    "from": IATA.get(codes[0], codes[0]) if codes else None,
                    "to": IATA.get(codes[1], codes[1]) if len(codes) > 1 else None,
                    "date": d,
                    "dep": times[0] if times else None,
                    "arr": times[1] if len(times) > 1 else None,
                    "raw": ln,
                }
            )
            continue
        # ferries:  "10 באוגוסט 2026: מפיקו (Cais do Pico) לסאו ז'ורז' (Velas) |
        #            יציאה ב-08:30, הגעה ב-09:20 | הזמנה 68898"
        if (
            re.search(r"מעבורת|Atlânticoline", ln) is None
            and " | " in ln
            and re.search(r"יציאה|הגעה", ln)
        ):
            ports = re.findall(r"\(([^)]+)\)", ln)
            times = re.findall(r"\b(\d{1,2}:\d{2})\b", ln)
            book = re.search(r"הזמנה\s+(\w+)", ln)
            d = heb_date(ln, year)
            if d and len(ports) >= 2:
                ferries.append(
                    {
                        "from": ports[0].strip(),
                        "to": ports[1].strip(),
                        "date": d,
                        "dep": times[0] if times else None,
                        "arr": times[1] if len(times) > 1 else None,
                        "booking": book.group(1) if book else None,
                        "raw": ln,
                    }
                )
                continue
        # lodging:  "3 באוגוסט – 4 באוגוסט | ליסבון: <name> (אצל <host>) | קוד: <code>"
        lm = re.match(r"(.+?)\s*[–-]\s*(.+?)\s*\|\s*([^:]+):\s*(.*)", ln)
        if lm and re.search(r"ב?" + "|".join(HEB_MONTHS), lm.group(1)):
            d1, d2 = heb_date(lm.group(1), year), heb_date(lm.group(2), year)
            place = lm.group(3).strip()
            rest = lm.group(4)
            host = re.search(r"אצל\s+([^)|]+)", rest)
            code = re.search(r"קוד:\s*(\w+)", rest)
            name = re.split(r"\s*\(אצל|\s*\|", rest)[0].strip()
            stays.append(
                {
                    "dateRange": [d1, d2] if d1 and d2 else None,
                    "place": place,
                    "name": name,
                    "host": host.group(1).strip() if host else None,
                    "code": code.group(1) if code else None,
                    "raw": ln,
                }
            )

    return {"year": year, "flights": flights, "ferries": ferries, "stays": stays}


# ----------------------------------------------------------------- AI extraction

AI_SCHEMA = {
    "type": "object",
    "properties": {
        "meta": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "start_date": {"type": "string"},
                "end_date": {"type": "string"},
            },
        },
        "places": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},  # Latin/local script
                    "lat": {"type": "number"},
                    "lon": {"type": "number"},
                },
                "required": ["name"],
            },
        },
        "timeline": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string"},  # transit | stay | layover | drama
                    "mode": {"type": "string"},  # plane | ferry (transit only)
                    "from": {"type": "string"},
                    "to": {"type": "string"},
                    "fog": {"type": "string"},  # drama only: place name where it went wrong
                    "stops": {
                        "type": "array",
                        "items": {"type": "string"},
                    },  # display names, origin -> dest
                    "date": {"type": "string"},
                    "dateRange": {"type": "array", "items": {"type": "string"}},
                    "flightNo": {"type": "string"},
                    "route": {"type": "string"},
                    "key": {"type": "string"},
                    "city": {"type": "string"},
                    "island": {"type": "string"},
                    "host": {"type": "string"},
                    "code": {"type": "string"},
                    "via": {"type": "string"},
                    "note": {"type": "string"},
                },
                "required": ["type"],
            },
        },
    },
    "required": ["timeline", "places"],
}

AI_PROMPT = """You are extracting a trip itinerary from trip logistics notes (often Hebrew).
Return JSON matching the schema. Rules:
- One `timeline` item per flight/ferry (type "transit", mode "plane"/"ferry"),
  per lodging (type "stay"), and one type "drama" if a flight was cancelled /
  diverted / returned. Chronological order.
- transit: `from` and `to` are PLACE names (the city, not the airport code);
  `stops` = [origin, destination] as display names in the notes' language;
  `date` YYYY-MM-DD; `flightNo`; `route` like "יציאה HH:MM · נחיתה HH:MM".
- drama: also set `from` (departure city) and `to` (the ORIGINAL intended
  destination it failed to reach) as PLACE names, same convention as transit;
  `fog` = the place name where the problem happened (usually same as `to`).
- stay: `dateRange` [checkin, checkout] YYYY-MM-DD; `city`; `island` (or the
  COUNTRY for mainland); `host`; `code` (a booking confirmation code, if any);
  `via` = the booking channel - "Airbnb" if the code looks like an Airbnb
  reservation code (e.g. starts "HM") or Airbnb is mentioned, "Booking.com" if
  that's mentioned, otherwise the hotel/site name if it's clearly a hotel, else
  omit; a short lowercase-ascii `key` ("krakow1", "poprad2"). `note`: at most
  ONE short sentence, or omit it.
- `places`: EVERY distinct place used anywhere (every from/to/city/island/
  country), each with `name` in Latin or local script (NOT a foreign
  transliteration - e.g. "Kraków" not "קרקוב", "Tel Aviv" not "תל אביב") and
  its `lat`,`lon` (your best known coordinates; a town centre is fine).
- Do NOT invent prose. No `lede`, `about`, `tags`.
"""


def parse_ai(text: str, model: str, api_key: str | None = None, log=print) -> dict | None:
    try:
        import gemini_util as gu
    except ImportError:
        return None
    try:
        return gu.generate_json(
            model,
            AI_PROMPT + "\n\n---\n\n" + text[:60000],
            schema=AI_SCHEMA,
            temperature=0.1,
            log=log,
        )
    except Exception as e:
        log(f"  AI extraction failed: {e}")
        return None


# ------------------------------------------------------------------- geocoding


def geocode(names: list[str], cache_path: pathlib.Path, hint: str = "") -> dict:
    try:
        import requests
    except ImportError:
        sys.exit("pip install requests")
    cache = {}
    if cache_path.exists():
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    for name in names:
        if not name or cache.get(name):  # retry names previously cached as None
            continue
        q = f"{name}, {hint}".strip(", ")
        try:
            r = requests.get(
                NOMINATIM,
                params={"q": q, "format": "json", "limit": 1},
                headers={"User-Agent": UA},
                timeout=20,
            )
            r.raise_for_status()
            hits = r.json()
            cache[name] = (
                {"lat": round(float(hits[0]["lat"]), 4), "lon": round(float(hits[0]["lon"]), 4)}
                if hits
                else None
            )
            print(f"  geocode  {name:24} -> {cache[name]}")
        except Exception as e:
            print(f"  geocode  {name:24} -> FAILED ({e})")
            cache[name] = None
        time.sleep(1.1)  # Nominatim: <= 1 req/sec
    cache_path.write_bytes((json.dumps(cache, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    return cache


def _gemini_coords(names: list[str], model: str, log=print) -> dict:
    try:
        import gemini_util as gu

        if not gu.have_key():
            return {}
        schema = {
            "type": "object",
            "properties": {
                "places": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "lat": {"type": "number"},
                            "lon": {"type": "number"},
                        },
                        "required": ["name", "lat", "lon"],
                    },
                }
            },
        }
        d = gu.generate_json(
            model,
            "Give best-known WGS84 coordinates (town/area centre is fine) for each place. "
            "JSON only.\n" + "\n".join(f"- {n}" for n in names),
            schema=schema,
            temperature=0,
            log=log,
        )
        return {
            p["name"]: {"lat": round(p["lat"], 4), "lon": round(p["lon"], 4)}
            for p in d.get("places", [])
            if p.get("lat") and p.get("lon")
        }
    except Exception as e:
        log(f"  gemini coord fallback failed: {e}")
        return {}


def fill_coords(
    spec: dict, hint: str, model: str, cache_path: pathlib.Path, log=print
) -> list[str]:
    """Resolve every location that still has a null lat/lon. Returns names left unresolved."""
    need = [v["label"] for v in spec.get("locations", {}).values() if not v.get("lat")]
    if not need:
        return []
    geo = geocode(need, cache_path, hint)
    still = [n for n in need if not (geo.get(n) or {}).get("lat")]
    if still:
        log(f"  {len(still)} place(s) unresolved by Nominatim - asking the model")
        gco = _gemini_coords(still, model, log)
        for n, c in gco.items():
            geo[n] = c
    for v in spec["locations"].values():
        c = geo.get(v["label"])
        if c and c.get("lat"):
            v["lat"], v["lon"] = c["lat"], c["lon"]
    return [v["label"] for v in spec["locations"].values() if not v.get("lat")]


# --------------------------------------------------------------------- assembly


def slug(s: str, taken: set) -> str:
    base = re.sub(r"[^a-z0-9]+", "", (s or "loc").lower()) or "loc"
    k, i = base, 1
    while k in taken:
        i += 1
        k = f"{base}{i}"
    taken.add(k)
    return k


def assemble(ai: dict | None, rx: dict, geo: dict | None = None) -> dict:
    """Prefer the AI structure; fall back to the regex structure."""
    geo = geo or {}
    spec = {"meta": {}, "locations": {}, "photos": {}, "timeline": []}
    loc_keys: dict[str, str] = {}
    taken: set = set()
    # coords the model already gave us, keyed by place name
    ai_coords = {
        p["name"]: {"lat": p["lat"], "lon": p["lon"]}
        for p in (ai.get("places", []) if ai else [])
        if p.get("lat") and p.get("lon")
    }

    def loc(name: str) -> str | None:
        if not name:
            return None
        if name not in loc_keys:
            key = slug(name, taken)
            loc_keys[name] = key
            c = ai_coords.get(name) or geo.get(name) or {"lat": None, "lon": None}
            spec["locations"][key] = {"label": name, "lat": c.get("lat"), "lon": c.get("lon")}
        return loc_keys[name]

    if ai and ai.get("timeline"):
        spec["meta"] = ai.get("meta", {})
        for it in ai["timeline"]:
            t = dict(it)
            if t.get("type") == "transit":
                if not t.get("stops"):
                    t["stops"] = [x for x in (t.get("from"), t.get("to")) if x]
                for f in ("from", "to"):
                    if t.get(f):
                        t[f] = loc(t[f])
            if t.get("type") == "drama":
                if t.get("fog") and not t.get("to"):
                    t["to"] = t["fog"]  # fog implies the intended destination
                if not t.get("fog") and t.get("to"):
                    t["fog"] = t["to"]
                for f in ("from", "to", "fog"):
                    if t.get(f):
                        t[f] = loc(t[f])
            if t.get("type") == "stay":
                if not t.get("key"):
                    t["key"] = slug(t.get("city") or "stay", taken)
                loc(t.get("city"))
            spec["timeline"].append(t)
            if t.get("type") in ("stay", "layover") and t.get("key"):
                spec["photos"].setdefault(t["key"], {"lodging": [], "trip": []})
        # any place the model listed but nothing referenced -> still register it
        for p in ai.get("places", []):
            loc(p.get("name"))
        # the model doesn't always fill drama.from/to/fog (they overlap awkwardly with
        # the free-text note) - infer them from the surrounding stops when missing:
        # `from` = wherever we were stuck (the nearest earlier stay/transit), `to` = the
        # nearest later stop that's actually a different place (what we were trying to reach)
        for i, it in enumerate(spec["timeline"]):
            if it.get("type") != "drama" or (it.get("from") and it.get("to")):
                continue
            frm = it.get("from")
            for prev in reversed(spec["timeline"][:i]):
                if prev.get("type") == "transit" and prev.get("to"):
                    frm = frm or prev["to"]
                    break
                if prev.get("type") in ("stay", "layover"):
                    frm = frm or loc_keys.get(prev.get("city"))
                    break
            to = it.get("to")
            for nxt in spec["timeline"][i + 1 :]:
                if nxt.get("type") == "transit" and nxt.get("to") and nxt.get("to") != frm:
                    to = to or nxt["to"]
                    break
                if nxt.get("type") in ("stay", "layover"):
                    k = loc_keys.get(nxt.get("city"))
                    if k and k != frm:
                        to = to or k
                        break
            it["from"], it["to"] = frm, to
            it["fog"] = it.get("fog") or to
        return spec

    # regex fallback: interleave flights/ferries/stays by date
    events = []
    for f in rx["flights"]:
        events.append(
            (
                f["date"] or "9999",
                "transit",
                {
                    "type": "transit",
                    "mode": "plane",
                    "from": loc(f["from"]),
                    "to": loc(f["to"]),
                    "date": f["date"],
                    "flightNo": f["flightNo"],
                    "route": (
                        f'יציאה {f["dep"]} · נחיתה {f["arr"]}' if f["dep"] and f["arr"] else ""
                    ),
                },
            )
        )
    for f in rx["ferries"]:
        events.append(
            (
                f["date"] or "9999",
                "transit",
                {
                    "type": "transit",
                    "mode": "ferry",
                    "from": loc(f["from"]),
                    "to": loc(f["to"]),
                    "date": f["date"],
                    "route": f'יציאה {f["dep"]} · הגעה {f["arr"]}' if f["dep"] and f["arr"] else "",
                },
            )
        )
    for s in rx["stays"]:
        rng = s.get("dateRange")
        events.append(
            (
                (rng[0] if rng else "9999"),
                "stay",
                {
                    "type": "stay",
                    "key": slug(s["place"], taken),
                    "city": s["place"],
                    "island": "",
                    "dates": f"{rng[0]}..{rng[1]}" if rng else "",
                    "dateRange": rng,
                    "host": s["name"] + (f" · {s['host']}" if s.get("host") else ""),
                    **({"code": s["code"]} if s.get("code") else {}),
                },
            )
        )
        loc(s["place"])
    events.sort(key=lambda e: (e[0], 0 if e[1] == "transit" else 1))
    for _, _, item in events:
        spec["timeline"].append(item)
        if item.get("type") == "stay":
            spec["photos"].setdefault(item["key"], {"lodging": [], "trip": []})
    if rx.get("year"):
        spec["meta"]["title"] = f"טיול {rx['year']}"
    return spec


# --------------------------------------------------------------------- day split


def _day_label(d) -> str:
    return f"{d.day}.{d.month}"


def _stay_to_days(stay: dict) -> list[dict]:
    rng = stay.get("dateRange")
    if not (isinstance(rng, list) and len(rng) == 2 and rng[0] and rng[1]):
        return [stay]  # can't split - leave as-is
    start, end = date.fromisoformat(rng[0]), date.fromisoformat(rng[1])
    n = max(1, (end - start).days)
    days = []
    for i in range(n):
        d = start + timedelta(days=i)
        item = {
            "type": "day",
            "key": stay["key"],
            "date": d.isoformat(),
            "dayIndex": i + 1,
            "dayCount": n,
            "isCheckIn": i == 0,
            "city": stay.get("city", ""),
            "island": stay.get("island", ""),
            "dates": _day_label(d),
        }
        if i == 0:
            for f in ("title", "host", "code", "airbnb", "about", "via"):
                if stay.get(f) is not None:
                    item[f] = stay[f]
            item["stayDates"] = stay.get("dates") or f"{rng[0]}..{rng[1]}"
        days.append(item)
    return days


def expand_days(spec: dict) -> dict:
    """Explode every multi-night `stay` into one `day` item per calendar date
    (checkout date belongs to the *next* stop, never double-counted). `layover`
    and `transit` items pass through untouched."""
    out = []
    for it in spec.get("timeline", []):
        out.extend(_stay_to_days(it) if it.get("type") == "stay" else [it])
    spec["timeline"] = out
    return spec


# ------------------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--docs",
        nargs="+",
        default=["*.pdf", "*.docx"],
        help="doc files or globs (default: every pdf/docx in the folder)",
    )
    ap.add_argument("--out", default=DRAFT)
    ap.add_argument("--ai", dest="ai", action="store_true", default=True)
    ap.add_argument("--no-ai", dest="ai", action="store_false")
    ap.add_argument("--model", default="gemini-3.6-flash")
    ap.add_argument("--no-geocode", dest="geocode", action="store_false", default=True)
    ap.add_argument(
        "--geocode-hint", default="", help="appended to every place name for the lat/lon lookup"
    )
    a = ap.parse_args()

    root = pathlib.Path(__file__).resolve().parent
    files: list[pathlib.Path] = []
    for pat in a.docs:
        p = pathlib.Path(pat)
        files += [
            pathlib.Path(x)
            for x in (glob.glob(str(root / pat)) if not p.is_absolute() else glob.glob(pat))
        ]
    files = [f for f in dict.fromkeys(files) if f.is_file()]
    if not files:
        sys.exit(f"no documents matched {a.docs}")
    print("reading:", ", ".join(f.name for f in files))

    text = "\n\n".join(read_doc(f) for f in files)

    rx = parse_docx_regex(text)
    print(
        f"  regex pass: {len(rx['flights'])} flights, "
        f"{len(rx['ferries'])} ferries, {len(rx['stays'])} stays"
    )

    ai = None
    if a.ai:
        import gemini_util as gu

        if not gu.have_key():
            print("  no GEMINI_API_KEY - using the regex parser only")
        else:
            ai = parse_ai(text, a.model)
            if ai:
                print(
                    f"  AI pass: {len(ai.get('timeline', []))} timeline items, "
                    f"{len(ai.get('places', []))} places"
                )

    spec = assemble(ai, rx)
    expand_days(spec)
    if a.geocode:
        fill_coords(spec, a.geocode_hint, a.model, root / GEO_CACHE)

    missing = [k for k, v in spec["locations"].items() if not v.get("lat")]
    (root / a.out).write_bytes(
        (json.dumps(spec, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    )
    print(
        f"\nwrote {a.out}  ({len(spec['timeline'])} timeline items, "
        f"{len(spec['locations'])} places)"
    )
    if missing:
        print(
            f"  ! no coordinates for: {', '.join(spec['locations'][k]['label'] for k in missing)}"
        )
    print("  review it, merge the good parts into trip_spec.json, then run gen_copy.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
