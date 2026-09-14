#!/usr/bin/env python3
"""
fetch_photos.py  —  Phase 2, step 1 of the trip-journal generator.

Pulls the photos you pick from a Google Photos album into a local folder, as
**web-size copies** (default 1600px long edge) — never the originals — plus a
manifest with capture times. select_photos.py then sorts and chooses them.

Uses the Google Photos **Picker API** (the current supported way): the script
opens a Google page, you tick the trip photos there and hit Done, and it
downloads a downscaled copy of each plus manifest.json.

One-time setup
--------------
1. console.cloud.google.com -> new project.
2. "APIs & Services" -> Enable APIs -> enable  "Photos Picker API".
3. "APIs & Services" -> OAuth consent screen -> External -> add yourself under
   "Test users".
4. "Credentials" -> Create credentials -> OAuth client ID -> **Desktop app** ->
   download the JSON, save it next to this script as  credentials.json .

Then:
    pip install -r requirements.txt
    python fetch_photos.py --out gphotos

First run pops a browser to authorise; the token is cached in token.json.

Output
------
    gphotos/IMG_xxxx.jpg ...     downscaled copies of the picked photos
    gphotos/manifest.json        {downscale_px, items:[{id,file,createTime,width,height,orig_width,orig_height}]}
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
import time

try:
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import AuthorizedSession, Request
    from google.oauth2.credentials import Credentials
except ImportError:
    sys.exit("run:  pip install google-auth google-auth-oauthlib requests")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SCOPES = ["https://www.googleapis.com/auth/photospicker.mediaitems.readonly"]
BASE = "https://photospicker.googleapis.com/v1"


def _ok(r):
    """raise_for_status, but show Google's error body (it says *why*) first."""
    if not r.ok:
        body = ""
        try:
            body = json.dumps(r.json().get("error", r.json()), ensure_ascii=False, indent=2)
        except Exception:
            body = r.text[:800]
        print(f"\nHTTP {r.status_code} from {r.request.method} {r.url}\n{body}\n", file=sys.stderr)
        if r.status_code in (403, 401):
            print("  most likely: the 'Photos Picker API' is not enabled on this project, or\n"
                  "  the granted token lacks the photospicker scope (delete token.json and re-run).\n"
                  "  Enable it:  https://console.cloud.google.com/apis/library/photospicker.googleapis.com\n",
                  file=sys.stderr)
        r.raise_for_status()
    return r


def _duration_seconds(s, default: float) -> float:
    m = re.match(r"([0-9.]+)s?$", str(s or "").strip())      # protobuf duration "3.5s"
    return float(m.group(1)) if m else default


def authorise(creds_path: pathlib.Path, token_path: pathlib.Path) -> AuthorizedSession:
    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not creds_path.exists():
                sys.exit(f"missing {creds_path} - see the setup notes at the top of this file")
            creds = InstalledAppFlow.from_client_secrets_file(
                str(creds_path), SCOPES).run_local_server(port=0)
        token_path.write_text(creds.to_json(), encoding="utf-8")
    return AuthorizedSession(creds)


def open_session(creds_path: pathlib.Path, token_path: pathlib.Path):
    """-> (AuthorizedSession, session dict with 'id' and 'pickerUri'). For the web app."""
    http = authorise(creds_path, token_path)
    return http, _ok(http.post(f"{BASE}/sessions", json={})).json()


def session_ready(http: AuthorizedSession, sid: str) -> bool:
    return bool(_ok(http.get(f"{BASE}/sessions/{sid}")).json().get("mediaItemsSet"))


def collect(http: AuthorizedSession, sid: str, out: pathlib.Path, size: int) -> list[dict]:
    """List + download the picked items; write manifest.json. Assumes session_ready() is True."""
    items = list_items(http, sid)
    manifest = download(http, items, out, size)
    try:
        http.delete(f"{BASE}/sessions/{sid}")
    except Exception:
        pass
    return manifest


def wait_for_pick(http: AuthorizedSession, session: dict) -> None:
    sid = session["id"]
    poll = _duration_seconds(session.get("pollingConfig", {}).get("pollInterval"), 3.0)
    deadline = time.time() + _duration_seconds(
        session.get("pollingConfig", {}).get("timeoutIn"), 600.0)
    print("\n  1. open this link, pick the trip photos, then click 'Done':\n")
    print("     " + session["pickerUri"] + "\n")
    print("  waiting for you to finish in the browser ", end="", flush=True)
    while time.time() < deadline:
        time.sleep(poll)
        s = _ok(http.get(f"{BASE}/sessions/{sid}"))
        if s.json().get("mediaItemsSet"):
            print(" got it.")
            return
        print(".", end="", flush=True)
    sys.exit("\ntimed out waiting for the picker")


def list_items(http: AuthorizedSession, sid: str) -> list[dict]:
    items, token = [], None
    while True:
        params = {"sessionId": sid, "pageSize": 100}
        if token:
            params["pageToken"] = token
        r = _ok(http.get(f"{BASE}/mediaItems", params=params))
        body = r.json()
        items.extend(body.get("mediaItems", []))
        token = body.get("nextPageToken")
        if not token:
            return items


def download(http: AuthorizedSession, items: list[dict], out: pathlib.Path, px: int) -> list[dict]:
    out.mkdir(parents=True, exist_ok=True)
    manifest, n, used = [], 0, set()
    for it in items:
        if it.get("type") != "PHOTO":
            continue
        mf = it.get("mediaFile", {})
        meta = mf.get("mediaFileMetadata", {})
        stem = re.sub(r"[^\w.\-]+", "_", mf.get("filename") or it["id"][:16])
        stem = pathlib.Path(stem).stem
        name = f"{stem}.jpg"
        i = 2
        while name in used:
            name = f"{stem}_{i}.jpg"
            i += 1
        used.add(name)
        # =wN-hN  -> fit inside NxN, re-encoded, auto-oriented, no EXIF. never =d (original).
        r = _ok(http.get(f'{mf["baseUrl"]}=w{px}-h{px}'))
        (out / name).write_bytes(r.content)
        n += 1
        manifest.append({
            "id": it["id"],
            "file": name,
            "createTime": it.get("createTime", ""),
            "orig_width": int(meta.get("width") or 0),
            "orig_height": int(meta.get("height") or 0),
        })
        print(f"    {name}  ({len(r.content)//1024} KB)")
    (out / "manifest.json").write_bytes((json.dumps(
        {"downscale_px": px, "count": n, "items": manifest},
        ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    return manifest


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="gphotos", help="folder for the downscaled photos + manifest.json")
    ap.add_argument("--size", type=int, default=1600, help="long-edge px of the downloaded copies")
    ap.add_argument("--credentials", default="credentials.json")
    ap.add_argument("--token", default="token.json")
    a = ap.parse_args()

    root = pathlib.Path(__file__).resolve().parent
    rel = lambda x: pathlib.Path(x) if pathlib.Path(x).is_absolute() else root / x

    http = authorise(rel(a.credentials), rel(a.token))
    session = _ok(http.post(f"{BASE}/sessions", json={})).json()
    try:
        wait_for_pick(http, session)
        items = list_items(http, session["id"])
        print(f"\n  2. {len(items)} item(s) picked; downloading {a.size}px copies to {a.out}/ ...")
        manifest = download(http, items, rel(a.out), a.size)
    finally:
        try:
            http.delete(f"{BASE}/sessions/{session['id']}")
        except Exception:
            pass

    print(f"\ndone - {len(manifest)} photo(s) in {a.out}/  (no originals downloaded)")
    print(f"next:  python select_photos.py --manifest {a.out}/manifest.json --media-dir {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
