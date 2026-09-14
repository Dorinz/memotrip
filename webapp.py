#!/usr/bin/env python3
"""
webapp.py — Phase 4 MVP: a local, single-user web front end for the trip-journal pipeline.

    pip install -r requirements.txt
    set GEMINI_API_KEY=...            (for parsing + copy; without it you get a thin page)
    python webapp.py                  ->  http://localhost:8000

Flow: paste a description + upload logistics docs -> the pipeline runs in the
background (parse_docs -> geocode -> gen_copy -> build_trip) -> preview the page ->
optionally "Add photos" (Google Photos Picker, reuses credentials.json / token.json)
-> select_photos + rebuild.

State lives in trips.db (sqlite) and webapp_data/<trip_id>/.
"""
from __future__ import annotations

try:
    import local_env  # noqa: F401  (loads .env)
except Exception:
    pass

import json
import os
import pathlib
import sqlite3
import threading
import time
import traceback
import uuid

import html as _html

import uvicorn
from fastapi import FastAPI, Form, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

import build_trip
import parse_docs
import gen_copy
import select_photos
import fetch_photos
import palette

ROOT = pathlib.Path(__file__).resolve().parent
DATA = ROOT / "webapp_data"
DB = ROOT / "trips.db"
TEMPLATE = (ROOT / "trip_template.html").read_text(encoding="utf-8")
MODEL = os.environ.get("TRIP_MODEL", "gemini-3.6-flash")
DATA.mkdir(exist_ok=True)

app = FastAPI(title="Trip Journal")
app.mount("/data", StaticFiles(directory=str(DATA)), name="data")

TPLDIR = ROOT / "webapp_templates"


def render(name: str, **marks) -> HTMLResponse:
    page = (TPLDIR / name).read_text(encoding="utf-8")
    for k, v in marks.items():
        page = page.replace("{{" + k + "}}", v)
    return HTMLResponse(page)


# ------------------------------------------------------------------------- db

def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


with db() as _c:
    _c.execute("""CREATE TABLE IF NOT EXISTS trips(
        id TEXT PRIMARY KEY, created TEXT, description TEXT, region_hint TEXT,
        status TEXT, stage TEXT, log TEXT, error TEXT,
        picker_uri TEXT, picker_sid TEXT)""")


def trip_dir(tid: str) -> pathlib.Path:
    return DATA / tid


def get_trip(tid: str):
    with db() as c:
        return c.execute("SELECT * FROM trips WHERE id=?", (tid,)).fetchone()


def update(tid: str, **fields):
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    with db() as c:
        c.execute(f"UPDATE trips SET {sets} WHERE id=?", (*fields.values(), tid))


def logline(tid: str, msg: str):
    row = get_trip(tid)
    log = (row["log"] or "") + msg + "\n"
    update(tid, log=log)
    print(f"[{tid[:8]}] {msg}")


# -------------------------------------------------------------------- pipeline

def _docs_text(tid: str) -> tuple[str, list[pathlib.Path]]:
    files = sorted((trip_dir(tid) / "docs").glob("*")) if (trip_dir(tid) / "docs").is_dir() else []
    text = "\n\n".join(parse_docs.read_doc(f) for f in files if f.is_file())
    return text, files


def run_build(tid: str):
    try:
        row = get_trip(tid)
        desc = row["description"] or ""
        hint = row["region_hint"] or ""
        key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")

        update(tid, status="running", stage="reading documents", error=None)
        text, files = _docs_text(tid)
        logline(tid, f"read {len(files)} document(s), {len(text)} chars")
        source = text + ("\n\n" + desc if desc else "")

        update(tid, stage="parsing itinerary")
        rx = parse_docs.parse_docx_regex(source)
        ai = parse_docs.parse_ai(source, MODEL, log=lambda m: logline(tid, m)) if key else None
        logline(tid, f"regex: {len(rx['flights'])}f/{len(rx['ferries'])}fe/{len(rx['stays'])}s"
                     + (f"; ai: {len(ai.get('timeline', []))} items" if ai else "; no ai"))

        spec = parse_docs.assemble(ai, rx)
        spec.setdefault("meta", {}).setdefault("tz_offset_hours", 0)
        for it in spec["timeline"]:
            if it.get("type") in ("stay", "layover") and it.get("key"):
                spec.setdefault("photos", {}).setdefault(it["key"], {"lodging": [], "trip": []})
        spec = parse_docs.expand_days(spec)          # one section per calendar day
        if not spec.get("timeline"):
            raise RuntimeError("no itinerary found — add more detail to the description or docs")

        # one color theme per trip, generated once and kept across rebuilds (e.g. adding photos)
        old_spec_path = trip_dir(tid) / "spec.json"
        if old_spec_path.exists():
            try:
                spec["theme"] = json.loads(old_spec_path.read_text(encoding="utf-8")).get("theme")
            except Exception:
                pass
        if not spec.get("theme"):
            spec["theme"] = palette.generate_palette(tid)
            logline(tid, f"color theme: accent {spec['theme']['turquoise']} / {spec['theme']['sea']}")

        update(tid, stage="finding the places on the map")
        unresolved = parse_docs.fill_coords(spec, hint, MODEL, ROOT / "geocode_cache.json",
                                            log=lambda m: logline(tid, m))
        if unresolved:
            spec.setdefault("_warnings", []).append("no coordinates for: " + ", ".join(unresolved))
            logline(tid, "! unresolved places: " + ", ".join(unresolved))

        if key:
            update(tid, stage="writing the story")
            try:
                gen_copy.generate_copy(spec, desc or "", text, model=MODEL,
                                       log=lambda m: logline(tid, m))
            except Exception as e:
                logline(tid, f"copy step skipped: {e}")
        else:
            logline(tid, "no GEMINI_API_KEY — page will have minimal text")

        update(tid, stage="building the page")
        (trip_dir(tid) / "spec.json").write_bytes(
            (json.dumps(spec, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        html = build_trip.build(spec, TEMPLATE)
        (trip_dir(tid) / "page.html").write_bytes(html.replace("\r\n", "\n").encode("utf-8"))

        warn = "\n".join(spec.get("_warnings", [])) or None
        update(tid, status="ready", stage="done", error=warn)
        logline(tid, "page built" + (" (with warnings)" if warn else ""))
    except Exception as e:
        logline(tid, "ERROR: " + str(e))
        update(tid, status="error", error=str(e) + "\n" + traceback.format_exc())


def run_photos(tid: str):
    try:
        key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        sid = get_trip(tid)["picker_sid"]
        update(tid, status="running", stage="waiting for your photo picks", error=None)
        http = fetch_photos.authorise(ROOT / "credentials.json", ROOT / "token.json")
        for _ in range(400):                       # ~10 min
            if fetch_photos.session_ready(http, sid):
                break
            time.sleep(2)
        else:
            raise RuntimeError("timed out waiting for the picker")

        update(tid, stage="downloading photos")
        gdir = trip_dir(tid) / "gphotos"
        manifest = fetch_photos.collect(http, sid, gdir, 1600)
        logline(tid, f"downloaded {len(manifest)} photo(s)")

        update(tid, stage="choosing the best shots")
        spec = json.loads((trip_dir(tid) / "spec.json").read_text(encoding="utf-8"))
        photos = select_photos.load_photos("picker", manifest=gdir / "manifest.json", media_dir=gdir)
        select_photos.select(photos, spec, trip_dir(tid) / "images" / "trips",
                             use_ai=bool(key), model=MODEL, log=lambda m: logline(tid, m))

        (trip_dir(tid) / "spec.json").write_bytes(
            (json.dumps(spec, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        html = build_trip.build(spec, TEMPLATE)
        (trip_dir(tid) / "page.html").write_bytes(html.replace("\r\n", "\n").encode("utf-8"))
        n = sum(len(v.get("trip", [])) for v in spec.get("photos", {}).values())
        n += sum(len(it.get("tripPhotos", [])) for it in spec.get("timeline", []) if it.get("type") == "day")
        update(tid, status="ready", stage="done", picker_uri=None, picker_sid=None)
        logline(tid, f"page rebuilt with {n} photo(s)")
    except Exception as e:
        logline(tid, "ERROR: " + str(e))
        update(tid, status="error", error=str(e) + "\n" + traceback.format_exc())


# --------------------------------------------------------------------- routes

@app.get("/", response_class=HTMLResponse)
def index():
    with db() as c:
        rows = c.execute("SELECT id, created, status, description FROM trips ORDER BY created DESC").fetchall()
    if rows:
        items = "".join(
            f'<li><a href="/trips/{r["id"]}">'
            f'{_html.escape((r["description"] or "(ללא תיאור)")[:70])}</a>'
            f'<span class="s"> · {r["created"]} · {r["status"]}</span></li>' for r in rows)
        trips_html = f'<h2>טיולים קודמים</h2><ul>{items}</ul>'
    else:
        trips_html = ""
    return render("index.html", TRIPS=trips_html)


@app.post("/trips")
async def create(description: str = Form(""), region_hint: str = Form(""),
                 docs: list[UploadFile] = None):
    tid = uuid.uuid4().hex
    d = trip_dir(tid)
    (d / "docs").mkdir(parents=True, exist_ok=True)
    (d / "images").mkdir(exist_ok=True)
    saved = 0
    for up in (docs or []):
        if up and up.filename:
            (d / "docs" / pathlib.Path(up.filename).name).write_bytes(await up.read())
            saved += 1
    if not description.strip() and not saved:
        return JSONResponse({"error": "add a description or at least one document"}, status_code=400)
    with db() as c:
        c.execute("INSERT INTO trips(id, created, description, region_hint, status, stage, log) "
                  "VALUES(?,?,?,?,?,?,?)",
                  (tid, time.strftime("%Y-%m-%d %H:%M"), description, region_hint,
                   "queued", "queued", ""))
    threading.Thread(target=run_build, args=(tid,), daemon=True).start()
    return RedirectResponse(f"/trips/{tid}", status_code=303)


@app.get("/trips/{tid}", response_class=HTMLResponse)
def trip_page(tid: str):
    if not get_trip(tid):
        return HTMLResponse("no such trip", status_code=404)
    return render("trip.html", TID=tid, TID8=tid[:8])


@app.get("/trips/{tid}/status")
def status(tid: str):
    row = get_trip(tid)
    if not row:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {k: row[k] for k in ("status", "stage", "log", "error", "picker_uri")}


@app.post("/trips/{tid}/photos/start")
def photos_start(tid: str):
    try:
        http, session = fetch_photos.open_session(ROOT / "credentials.json", ROOT / "token.json")
        update(tid, picker_uri=session["pickerUri"], picker_sid=session["id"])
        return {"picker_uri": session["pickerUri"]}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/trips/{tid}/photos/finish")
def photos_finish(tid: str):
    if not get_trip(tid)["picker_sid"]:
        return JSONResponse({"error": "start the picker first"}, status_code=400)
    threading.Thread(target=run_photos, args=(tid,), daemon=True).start()
    return {"ok": True}


@app.post("/trips/{tid}/rerun")
def rerun(tid: str):
    threading.Thread(target=run_build, args=(tid,), daemon=True).start()
    return RedirectResponse(f"/trips/{tid}", status_code=303)


@app.post("/trips/{tid}/theme/reroll")
def reroll_theme(tid: str):
    """New random color theme for this trip, without re-running the whole pipeline."""
    spec_path = trip_dir(tid) / "spec.json"
    if not spec_path.exists():
        return JSONResponse({"error": "build the page first"}, status_code=400)
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    spec["theme"] = palette.generate_palette(uuid.uuid4().hex)   # fresh random seed, not the trip id
    spec_path.write_bytes((json.dumps(spec, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    html = build_trip.build(spec, TEMPLATE)
    (trip_dir(tid) / "page.html").write_bytes(html.replace("\r\n", "\n").encode("utf-8"))
    return {"ok": True}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
