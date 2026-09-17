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
import re
import secrets
import shutil
import time
import traceback
import urllib.parse
import uuid

import html as _html

import bcrypt
import uvicorn
from fastapi import FastAPI, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

import build_trip
import parse_docs
import gen_copy
import select_photos
import fetch_photos
import palette
import config
import db as _db
import tasks

ROOT = pathlib.Path(__file__).resolve().parent
DATA = ROOT / "webapp_data"
TEMPLATE = (ROOT / "trip_template.html").read_text(encoding="utf-8")
MODEL = os.environ.get("TRIP_MODEL", "gemini-3.6-flash")
DATA.mkdir(exist_ok=True)

app = FastAPI(title="MemoTrip")
app.mount("/data", StaticFiles(directory=str(DATA)), name="data")

# session cookie signing key - generated once, persisted locally (gitignored)
# so logins survive a restart; SESSION_SECRET in .env overrides it if set.
_secret_path = ROOT / "session_secret.txt"
if not _secret_path.exists():
    _secret_path.write_text(secrets.token_hex(32), encoding="utf-8")
SESSION_SECRET = os.environ.get("SESSION_SECRET") or _secret_path.read_text(encoding="utf-8").strip()
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, same_site="lax")

TPLDIR = ROOT / "webapp_templates"


def render(name: str, **marks) -> HTMLResponse:
    page = (TPLDIR / name).read_text(encoding="utf-8")
    for k, v in marks.items():
        page = page.replace("{{" + k + "}}", v)
    return HTMLResponse(page)


# ------------------------------------------------------------------------- db
#
# db.py picks the backend: sqlite (trips.db, exactly as before) for local dev,
# or Postgres when DATABASE_URL is set (prod / Cloud SQL). db() is kept here
# as a same-named wrapper so every existing `with db() as c: c.execute(...)`
# call site below needs no changes.

def db():
    return _db.db()


_db.init_schema()


def trip_dir(tid: str) -> pathlib.Path:
    return DATA / tid


def get_trip(tid: str):
    with db() as c:
        return c.execute("SELECT * FROM trips WHERE id=?", (tid,)).fetchone()


# ----------------------------------------------------------------------- auth

def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(pw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


def get_user_by_email(email: str):
    with db() as c:
        return c.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def get_user(uid: int):
    with db() as c:
        return c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()


def current_user(request: Request):
    uid = request.session.get("user_id")
    return get_user(uid) if uid else None


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

        update(tid, status="running", stage="קורא את המסמכים", error=None)
        text, files = _docs_text(tid)
        logline(tid, f"read {len(files)} document(s), {len(text)} chars")
        source = text + ("\n\n" + desc if desc else "")

        update(tid, stage="מנתח את המסלול")
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
            raise RuntimeError("יצירת הדף נכשלה 😔 נסה שוב.")

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

        update(tid, stage="שולף מיקומים ומפות")
        unresolved = parse_docs.fill_coords(spec, hint, MODEL, ROOT / "geocode_cache.json",
                                            log=lambda m: logline(tid, m))
        if unresolved:
            spec.setdefault("_warnings", []).append("no coordinates for: " + ", ".join(unresolved))
            logline(tid, "! unresolved places: " + ", ".join(unresolved))

        if key:
            update(tid, stage="כותב את סיפור המסע")
            try:
                gen_copy.generate_copy(spec, desc or "", text, model=MODEL,
                                       log=lambda m: logline(tid, m))
            except Exception as e:
                logline(tid, f"copy step skipped: {e}")
        else:
            logline(tid, "no GEMINI_API_KEY — page will have minimal text")

        # a rerun rebuilds the spec from scratch (fresh parse of the docs/description),
        # which has no memory of photos picked on an earlier run - but the actual
        # downloaded photos are still sitting in gphotos/ from that earlier pick, so
        # re-select from them now instead of making the user go back to Google Photos
        if (trip_dir(tid) / "gphotos" / "manifest.json").is_file():
            update(tid, stage="בוחר את התמונות הכי טובות")
            _select_from_local_photos(tid, spec)

        update(tid, stage="בונה את דף המסע")
        (trip_dir(tid) / "spec.json").write_bytes(
            (json.dumps(spec, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        html = build_trip.build(spec, TEMPLATE)
        (trip_dir(tid) / "page.html").write_bytes(html.replace("\r\n", "\n").encode("utf-8"))

        warn = "\n".join(spec.get("_warnings", [])) or None
        if warn:
            logline(tid, "note: " + warn)
        logline(tid, "page built")

        # if photo access was already connected on the landing page, try to pick it
        # up right away (short wait - this must never fail the whole trip); if the
        # pick isn't finished yet, the page still goes "ready" and the trip page's
        # own "add photos" button covers finishing it later.
        row2 = get_trip(tid)
        if row2 and row2["picker_sid"]:
            update(tid, stage="ממתין לבחירת התמונות")
            try:
                http = fetch_photos.authorise_for_user(row2["user_id"], config.OAUTH_WEB_CLIENT_PATH)
                if _wait_for_pick(row2["picker_sid"], http, 90):
                    _download_and_select(tid, http, row2["picker_sid"])
                    update(tid, status="ready", stage="הושלם", error=None, picker_uri=None, picker_sid=None)
                else:
                    logline(tid, "still waiting on your Google Photos pick - the page is ready; "
                                 "use \"add photos\" on it once you're done picking")
                    update(tid, status="ready", stage="הושלם", error=warn)
            except Exception as e:
                logline(tid, f"photo step skipped: {e}")
                update(tid, status="ready", stage="הושלם", error=warn)
        else:
            update(tid, status="ready", stage="הושלם", error=warn)
    except Exception as e:
        # the full traceback goes to the log (console + DB) for debugging; the
        # user-facing error banner only ever shows the short message itself
        logline(tid, "ERROR: " + str(e) + "\n" + traceback.format_exc())
        update(tid, status="error", error=str(e))


def _wait_for_pick(sid: str, http, timeout_s: float) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if fetch_photos.session_ready(http, sid):
            return True
        time.sleep(2)
    return False


def _select_from_local_photos(tid: str, spec: dict) -> None:
    """(Re-)run photo selection against whatever's already downloaded in
    gphotos/ - shared by the first download and every later rerun, so the
    user is never sent back to the Google Photos picker just to rebuild."""
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    gdir = trip_dir(tid) / "gphotos"
    photos = select_photos.load_photos("picker", manifest=gdir / "manifest.json", media_dir=gdir)
    select_photos.select(photos, spec, trip_dir(tid) / "images" / "trips",
                         use_ai=bool(key), model=MODEL, log=lambda m: logline(tid, m))


def _download_and_select(tid: str, http, sid: str):
    update(tid, stage="מוריד את התמונות")
    gdir = trip_dir(tid) / "gphotos"
    manifest = fetch_photos.collect(http, sid, gdir, 1600)
    logline(tid, f"downloaded {len(manifest)} photo(s)")

    update(tid, stage="בוחר את התמונות הכי טובות")
    spec = json.loads((trip_dir(tid) / "spec.json").read_text(encoding="utf-8"))
    _select_from_local_photos(tid, spec)

    (trip_dir(tid) / "spec.json").write_bytes(
        (json.dumps(spec, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    html = build_trip.build(spec, TEMPLATE)
    (trip_dir(tid) / "page.html").write_bytes(html.replace("\r\n", "\n").encode("utf-8"))
    n = sum(len(v.get("trip", [])) for v in spec.get("photos", {}).values())
    n += sum(len(it.get("tripPhotos", [])) for it in spec.get("timeline", []) if it.get("type") == "day")
    logline(tid, f"page rebuilt with {n} photo(s)")


def run_photos(tid: str):
    """The trip page's manual 'add photos' button - the user is actively watching,
    so it's fine to wait longer and fail the trip if the pick never completes."""
    try:
        row = get_trip(tid)
        sid = row["picker_sid"]
        update(tid, status="running", stage="ממתין לבחירת התמונות", error=None)
        http = fetch_photos.authorise_for_user(row["user_id"], config.OAUTH_WEB_CLIENT_PATH)
        if not _wait_for_pick(sid, http, 600):
            raise RuntimeError("פג הזמן להמתנה לבחירת התמונות ב-Google Photos")
        _download_and_select(tid, http, sid)
        update(tid, status="ready", stage="הושלם", picker_uri=None, picker_sid=None)
    except Exception as e:
        # the full traceback goes to the log (console + DB) for debugging; the
        # user-facing error banner only ever shows the short message itself
        logline(tid, "ERROR: " + str(e) + "\n" + traceback.format_exc())
        update(tid, status="error", error=str(e))


# --------------------------------------------------------------------- routes

STATUS_LABELS = {"queued": "בתור", "running": "מעבד...", "ready": "מוכן", "error": "שגיאה"}


def trip_title(tid: str, row) -> str:
    """'YYYY-MM Region' (e.g. "2026-08 האיים האזוריים") once the trip has a
    built spec with a month + region; before that (still queued/running, or an
    older/failed build missing those fields) falls back to the DB row's own
    creation month + the free-text description, so the list is never blank."""
    spec_path = trip_dir(tid) / "spec.json"
    month, region = "", ""
    if spec_path.is_file():
        try:
            spec = json.loads(spec_path.read_text(encoding="utf-8"))
            month = ((spec.get("meta") or {}).get("start_date") or "")[:7]
            region = (spec.get("hero") or {}).get("region") or ""
        except Exception:
            pass
    if not month:
        month = (row["created"] or "")[:7]
    if region:
        return f"{month} {region}"
    return (row["description"] or "(ללא תיאור)")[:70]


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    user = current_user(request)
    if user:
        with db() as c:
            rows = c.execute("SELECT id, created, status, description FROM trips "
                              "WHERE user_id=? ORDER BY created DESC", (user["id"],)).fetchall()
        cards = "".join(
            f'<div class="trip-card">'
            f'<a class="t-link" href="/trips/{r["id"]}">'
            f'<div class="t"><div class="desc">{_html.escape(trip_title(r["id"], r))}</div>'
            f'<div class="date mono">{r["created"]}</div></div>'
            f'<span class="pill {r["status"]}">{STATUS_LABELS.get(r["status"], r["status"])}</span>'
            f'</a>'
            f'<button type="button" class="del-btn" data-tid="{r["id"]}" '
            f'title="מחיקת הטיול" aria-label="מחיקת הטיול">🗑</button>'
            f'</div>' for r in rows)
        trips_html = (f'<div class="trips" id="trips"><h2>הטיולים שלך</h2>'
                       + (cards or '<p class="hint">עדיין אין טיולים - תיצור/י אחד למטה.</p>') + '</div>')
        display = user["display_name"] or user["email"]
        initial = display.strip()[:1].upper() if display.strip() else "?"
        account_html = (
            f'<div class="account-corner" title="{_html.escape(display)}">'
            f'<button type="button" class="avatar" id="avatarBtn" aria-haspopup="true" '
            f'aria-expanded="false">{_html.escape(initial)}</button>'
            f'<div class="account-menu" id="accountMenu" hidden>'
            f'<a href="#trips" class="menu-item" id="myTripsLink">הטיולים שלי</a>'
            f'<button type="button" class="menu-item logout" id="logoutBtn">התנתקות</button>'
            f'</div></div>')
    else:
        trips_html = ""
        account_html = (
            '<div class="auth-corner">'
            '<a href="/login" class="auth-pill">התחברות/הרשמה</a>'
            '<div class="guest-hint">בשימוש כאורח - הטיול לא יישמר לחשבון</div>'
            '</div>')
    return render("index.html", TRIPS=trips_html, ACCOUNT=account_html)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, signup: str = "", error: str = ""):
    if current_user(request):
        return RedirectResponse("/", status_code=303)
    is_signup = bool(signup)
    return render(
        "login.html",
        TAB_LOGIN_CLASS=("" if is_signup else "active"),
        TAB_SIGNUP_CLASS=("active" if is_signup else ""),
        FORM_ACTION=("/signup" if is_signup else "/login"),
        PW_AUTOCOMPLETE=("new-password" if is_signup else "current-password"),
        PW_HINT=('<div class="hint-small">לפחות 6 תווים.</div>' if is_signup else ""),
        NAME_FIELD=('<label>שם / כינוי</label>'
                    '<input type="text" name="name" required autocomplete="nickname">'
                    if is_signup else ""),
        SUBMIT_LABEL=("יצירת חשבון" if is_signup else "התחברות"),
        ERROR=(f'<div class="err-banner show">{_html.escape(error)}</div>' if error else ""))


@app.post("/login")
def login_submit(request: Request, email: str = Form(...), password: str = Form(...)):
    u = get_user_by_email(email.strip().lower())
    if not u or not verify_password(password, u["password_hash"]):
        return RedirectResponse(
            "/login?error=" + urllib.parse.quote("אימייל או סיסמה שגויים."), status_code=303)
    request.session["user_id"] = u["id"]
    return RedirectResponse("/", status_code=303)


@app.post("/signup")
def signup_submit(request: Request, email: str = Form(...), password: str = Form(...),
                  name: str = Form("")):
    email = email.strip().lower()
    name = name.strip()
    if not EMAIL_RE.match(email) or len(password) < 6 or not name:
        return RedirectResponse(
            "/login?signup=1&error=" + urllib.parse.quote("שם, אימייל תקין, וסיסמה (6+ תווים)."),
            status_code=303)
    if get_user_by_email(email):
        return RedirectResponse(
            "/login?signup=1&error=" + urllib.parse.quote("כבר יש חשבון עם האימייל הזה."), status_code=303)
    with db() as c:
        c.execute("INSERT INTO users(email, password_hash, display_name, created) VALUES(?,?,?,?)",
                  (email, hash_password(password), name, time.strftime("%Y-%m-%d %H:%M")))
    request.session["user_id"] = get_user_by_email(email)["id"]
    return RedirectResponse("/", status_code=303)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=303)


@app.get("/oauth/photos/start")
def oauth_photos_start(request: Request, next: str = "/"):
    """Kicks off the per-user Google Photos OAuth flow - each user connects
    their own Google account (stored in the photo_accounts table), replacing
    the old shared token.json. Requires login first.

    OPERATOR NOTE: credentials_web.json must be a Web application OAuth
    client in Google Cloud Console (Credentials -> Create credentials ->
    OAuth client ID -> Web application) - NOT the Desktop app type used by
    fetch_photos.py's standalone CLI - with both
    http://localhost:8000/oauth/photos/callback and the production
    {PUBLIC_BASE_URL}/oauth/photos/callback registered as Authorized redirect
    URIs. This is a manual Google Cloud Console step; it can't be done from
    code."""
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    state = secrets.token_urlsafe(24)
    url, code_verifier = fetch_photos.build_auth_url(
        config.OAUTH_WEB_CLIENT_PATH,
        f"{config.PUBLIC_BASE_URL}/oauth/photos/callback",
        state)
    request.session["photos_oauth_state"] = state
    request.session["photos_oauth_next"] = next
    request.session["photos_oauth_verifier"] = code_verifier
    return RedirectResponse(url, status_code=303)


@app.get("/oauth/photos/callback")
def oauth_photos_callback(request: Request, code: str = "", state: str = ""):
    expected = request.session.pop("photos_oauth_state", None)
    next_url = request.session.pop("photos_oauth_next", "/") or "/"
    verifier = request.session.pop("photos_oauth_verifier", None)
    user = current_user(request)
    if not user or not state or not expected or state != expected or not verifier:
        return RedirectResponse("/login", status_code=303)
    try:
        creds = fetch_photos.exchange_code(
            config.OAUTH_WEB_CLIENT_PATH,
            f"{config.PUBLIC_BASE_URL}/oauth/photos/callback",
            code, verifier)
        fetch_photos.save_user_credentials(user["id"], creds)
    except Exception as e:
        return HTMLResponse(f"Google Photos connection failed: {_html.escape(str(e))}", status_code=500)
    return RedirectResponse(next_url, status_code=303)


@app.post("/photos/session")
def photos_session(request: Request):
    """Open a Google Photos picker session ahead of trip creation, so the user
    can grant access / start picking right from the landing page. Not tied to
    any trip yet - the returned sid rides along with the /trips form post.

    Requires a logged-in user with Google Photos already connected (each user
    authorises their own Google account - see /oauth/photos/start); a guest
    or an unconnected account gets a JSON error the front end redirects on."""
    user = current_user(request)
    if not user:
        return JSONResponse({"error": "login required", "login_url": "/login"}, status_code=401)
    try:
        http, session = fetch_photos.open_session_for_user(user["id"], config.OAUTH_WEB_CLIENT_PATH)
        return {"picker_uri": session["pickerUri"], "sid": session["id"]}
    except fetch_photos.PhotosNotConnected:
        return JSONResponse({"error": "connect Google Photos first",
                              "connect_url": "/oauth/photos/start?next=/"}, status_code=401)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/trips")
async def create(request: Request, description: str = Form(""), region_hint: str = Form(""),
                 picker_sid: str = Form(""), docs: list[UploadFile] = None):
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
    user = current_user(request)                     # None for a guest - trip stays unowned
    with db() as c:
        c.execute("INSERT INTO trips(id, created, description, region_hint, status, stage, log, "
                  "picker_sid, user_id) VALUES(?,?,?,?,?,?,?,?,?)",
                  (tid, time.strftime("%Y-%m-%d %H:%M"), description, region_hint,
                   "queued", "queued", "", picker_sid or None, user["id"] if user else None))
    tasks.enqueue("build", tid)
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
def photos_start(tid: str, request: Request):
    user = current_user(request)
    if not user:
        return JSONResponse({"error": "login required", "login_url": "/login"}, status_code=401)
    try:
        http, session = fetch_photos.open_session_for_user(user["id"], config.OAUTH_WEB_CLIENT_PATH)
        update(tid, picker_uri=session["pickerUri"], picker_sid=session["id"])
        return {"picker_uri": session["pickerUri"]}
    except fetch_photos.PhotosNotConnected:
        return JSONResponse({"error": "connect Google Photos first",
                              "connect_url": f"/oauth/photos/start?next=/trips/{tid}"}, status_code=401)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/trips/{tid}/photos/finish")
def photos_finish(tid: str, request: Request):
    user = current_user(request)
    if not user:
        return JSONResponse({"error": "login required", "login_url": "/login"}, status_code=401)
    if not get_trip(tid)["picker_sid"]:
        return JSONResponse({"error": "start the picker first"}, status_code=400)
    tasks.enqueue("photos", tid)
    return {"ok": True}


@app.post("/trips/{tid}/rerun")
def rerun(tid: str):
    tasks.enqueue("build", tid)
    return RedirectResponse(f"/trips/{tid}", status_code=303)


@app.post("/trips/{tid}/delete")
def delete_trip(request: Request, tid: str):
    """Permanently remove a trip: the DB row (so it's gone from "my trips")
    and its whole webapp_data/<tid>/ folder (spec, photos, docs, page.html)."""
    row = get_trip(tid)
    if not row:
        return JSONResponse({"error": "not found"}, status_code=404)
    user = current_user(request)
    if not user or row["user_id"] != user["id"]:
        return JSONResponse({"error": "not allowed"}, status_code=403)

    with db() as c:
        c.execute("DELETE FROM trips WHERE id=?", (tid,))

    # tid matched a real row fetched above, but a filesystem-wide delete still
    # gets an explicit guard on principle (see: the empty-tid rmtree incident in
    # BUILD_README/session notes) - never rmtree a path built from an unverified value
    assert tid and len(tid) >= 8, "refusing to remove an unexpected trip_dir path"
    d = trip_dir(tid)
    try:
        if d.is_dir():
            shutil.rmtree(d)
    except Exception as e:
        print(f"[{tid[:8]}] warning: trip row deleted but folder cleanup failed: {e}")

    return {"ok": True}


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


# ------------------------------------------------------------- internal tasks
#
# Targets for tasks.enqueue()'s Cloud Tasks mode: Cloud Tasks calls these
# synchronously (Cloud Run allocates CPU for the duration of the request,
# unlike a scaled-to-zero idle instance) instead of the dev-mode
# threading.Thread fallback. Protected by verifying the OIDC token Cloud
# Tasks attaches to the request - see tasks.py's oidc_token config.

def _verify_task_auth(authorization: str, expected_audience: str) -> None:
    if not config.CLOUD_TASKS_QUEUE:
        return   # local dev: no GCP infra configured - unused, the thread fallback never calls these
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=403, detail="missing task auth")
    from google.auth.transport import requests as g_requests
    from google.oauth2 import id_token
    token = authorization[len("Bearer "):]
    try:
        claims = id_token.verify_oauth2_token(token, g_requests.Request(), audience=expected_audience)
    except Exception:
        raise HTTPException(status_code=403, detail="invalid task auth")
    if config.TASKS_INVOKER_SA and claims.get("email") != config.TASKS_INVOKER_SA:
        raise HTTPException(status_code=403, detail="unexpected invoker")


@app.post("/internal/tasks/build/{tid}")
def internal_task_build(tid: str, authorization: str = Header(default="")):
    # audience is recomputed from config, not read off the request: behind
    # Cloud Run's proxy, request.url can report scheme/host differently from
    # the public https URL Cloud Tasks actually signed the OIDC token for,
    # which made this check fail with a 403 for every dispatch.
    _verify_task_auth(authorization, f"{config.PUBLIC_BASE_URL}/internal/tasks/build/{tid}")
    run_build(tid)
    return {"ok": True}


@app.post("/internal/tasks/photos/{tid}")
def internal_task_photos(tid: str, authorization: str = Header(default="")):
    _verify_task_auth(authorization, f"{config.PUBLIC_BASE_URL}/internal/tasks/photos/{tid}")
    run_photos(tid)
    return {"ok": True}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
