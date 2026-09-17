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
import datetime
import json
import pathlib
import re
import sys
import time

try:
    from google_auth_oauthlib.flow import InstalledAppFlow, Flow
    from google.auth.transport.requests import AuthorizedSession, Request
    from google.auth.exceptions import RefreshError
    from google.oauth2.credentials import Credentials
except ImportError:
    sys.exit("run:  pip install google-auth google-auth-oauthlib requests")


class PhotosNotConnected(Exception):
    """Raised by authorise_for_user() when a user hasn't connected their own
    Google Photos account yet (no photo_accounts row) - callers should catch
    this and send the user to /oauth/photos/start."""

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


# --------------------------------------------------------- per-user (web) oauth
#
# authorise() above is the single-shared-account **desktop app** flow used by
# this script's own CLI (main(), below) - one token.json for whoever runs the
# script locally. The functions in this section back the *web app*
# (webapp.py) instead: each logged-in user connects their own Google account
# via a server-side OAuth redirect, and their tokens are stored per-user in
# the photo_accounts DB table (see db.py) rather than in a shared token.json.
#
# A single Google Cloud OAuth client can only be one type, so this needs a
# *separate*, Web-application-type OAuth client from the Desktop-type one
# used by authorise() above - see credentials_web.json and the operator note
# next to the /oauth/photos/start route in webapp.py.

def build_auth_url(client_secrets_path: pathlib.Path, redirect_uri: str, state: str) -> tuple[str, str]:
    """-> (auth_url, code_verifier). access_type="offline" is required to get
    a refresh_token back.

    google-auth-oauthlib enables PKCE by default: authorization_url()
    generates a code_verifier and embeds its code_challenge in the returned
    URL, but the verifier only lives on this Flow object in memory - it is
    NOT recoverable from the URL or from Google. The caller MUST stash
    code_verifier somewhere that survives until the callback request (the
    session, alongside state) and pass it back into exchange_code(), or
    Google rejects the token exchange with "invalid_grant: Missing code
    verifier" (this bit us the first time through)."""
    flow = Flow.from_client_secrets_file(str(client_secrets_path), SCOPES, redirect_uri=redirect_uri)
    url, _ = flow.authorization_url(
        access_type="offline", include_granted_scopes="true", prompt="consent", state=state)
    return url, flow.code_verifier


def exchange_code(client_secrets_path: pathlib.Path, redirect_uri: str, code: str,
                   code_verifier: str) -> Credentials:
    """Completes the flow server-side once Google redirects back with ?code=...
    code_verifier must be the same one returned by build_auth_url() for this
    same login attempt (see its docstring - PKCE)."""
    flow = Flow.from_client_secrets_file(str(client_secrets_path), SCOPES, redirect_uri=redirect_uri)
    flow.code_verifier = code_verifier
    flow.fetch_token(code=code)
    return flow.credentials


def _web_client_info(client_secrets_path: pathlib.Path) -> tuple[str, str]:
    """-> (client_id, client_secret) from a Web-application-type OAuth client
    JSON (top-level key "web" - a Desktop-type client's file uses "installed"
    instead, which is why authorise() and this per-user flow use separate
    credentials files)."""
    data = json.loads(pathlib.Path(client_secrets_path).read_text(encoding="utf-8"))
    info = data.get("web") or data.get("installed") or {}
    return info["client_id"], info["client_secret"]


def save_user_credentials(owner_key: str, creds: Credentials) -> None:
    """Upsert a photo_accounts row from a fresh Credentials object - call
    right after exchange_code(), and again whenever authorise_for_user()
    refreshes an expired access token. owner_key is "user:<id>" for a
    logged-in account or "guest:<random>" for an anonymous session (see
    webapp.photo_owner_key) - not a users.id FK, so a guest can connect their
    own Google Photos without ever creating a MemoTrip account."""
    import db as _db
    expiry = creds.expiry.isoformat() if creds.expiry else None
    with _db.db() as c:
        c.execute("""INSERT INTO photo_accounts(owner_key, refresh_token, access_token,
                     token_expiry, granted) VALUES(?,?,?,?,?)
                     ON CONFLICT(owner_key) DO UPDATE SET
                     refresh_token=excluded.refresh_token,
                     access_token=excluded.access_token,
                     token_expiry=excluded.token_expiry,
                     granted=excluded.granted""",
                  (owner_key, creds.refresh_token, creds.token, expiry,
                   time.strftime("%Y-%m-%d %H:%M")))


def authorise_for_user(owner_key: str, client_secrets_path: pathlib.Path) -> AuthorizedSession:
    """Loads this owner's stored Google Photos tokens, refreshing if expired,
    and returns a ready-to-use AuthorizedSession. Raises PhotosNotConnected
    if there's no photo_accounts row yet for this owner_key (this also
    naturally covers an old guest trip predating per-guest identities, whose
    photo_owner_key is None - "WHERE owner_key=NULL" matches no row in either
    sqlite or Postgres)."""
    import db as _db
    with _db.db() as c:
        row = c.execute("SELECT * FROM photo_accounts WHERE owner_key=?", (owner_key,)).fetchone()
    if not row or not row["refresh_token"]:
        raise PhotosNotConnected(f"{owner_key} has not connected Google Photos")
    client_id, client_secret = _web_client_info(client_secrets_path)
    expiry = datetime.datetime.fromisoformat(row["token_expiry"]) if row["token_expiry"] else None
    creds = Credentials(
        token=row["access_token"], refresh_token=row["refresh_token"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id, client_secret=client_secret, scopes=SCOPES,
        expiry=expiry)
    if not creds.valid:
        try:
            creds.refresh(Request())
        except RefreshError:
            # the stored refresh token is dead - either revoked at
            # myaccount.google.com/permissions, or (very likely while this
            # app is still unverified with Google) it hit Google's 7-day
            # refresh-token expiry for apps in "Testing" publishing status.
            # Either way, drop the stale row and make the caller treat this
            # exactly like "never connected", so the user is sent back
            # through /oauth/photos/start for a fresh consent instead of
            # seeing a raw Google error.
            import db as _db
            with _db.db() as c:
                c.execute("DELETE FROM photo_accounts WHERE owner_key=?", (owner_key,))
            raise PhotosNotConnected(f"{owner_key}'s Google Photos token was revoked/expired")
        save_user_credentials(owner_key, creds)
    return AuthorizedSession(creds)


def open_session_for_user(owner_key: str, client_secrets_path: pathlib.Path):
    """-> (AuthorizedSession, session dict with 'id' and 'pickerUri'). Per-
    owner equivalent of open_session() above for the web app - raises
    PhotosNotConnected if this owner hasn't connected Google Photos yet.

    authorise_for_user() only catches a *dead* refresh token when its own
    proactive check (creds.valid, based on the locally-stored expiry) already
    knew a refresh was needed. If the stored token still looks unexpired
    locally but Google has actually revoked it already, creds.valid is True,
    authorise_for_user() skips its own refresh, and it's *this* request below
    that gets a 401 from Google - AuthorizedSession then silently tries its
    own refresh-and-retry, which raises the exact same RefreshError, just one
    level down from where the first fix caught it. Wrap this call too."""
    http = authorise_for_user(owner_key, client_secrets_path)
    try:
        return http, _ok(http.post(f"{BASE}/sessions", json={})).json()
    except RefreshError:
        import db as _db
        with _db.db() as c:
            c.execute("DELETE FROM photo_accounts WHERE owner_key=?", (owner_key,))
        raise PhotosNotConnected(f"{owner_key}'s Google Photos token was revoked/expired (during use)")


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
