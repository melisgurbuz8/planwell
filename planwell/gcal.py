"""Google Calendar access with the standard library only.

One-time login (installed-app OAuth, loopback redirect, PKCE):

    python3 -m planwell.gcal          # opens the browser, saves tokens, lists next 7 days

Needs secrets/google_client.json: the "Desktop app" OAuth client JSON downloaded from
Google Cloud Console (top-level key "installed"). Tokens go to secrets/google_tokens.json
and are refreshed automatically.

The scope is read-only today. To add calendar write-back later, append WRITE_SCOPE to SCOPES
(the stored token's scopes are checked, so the user will simply be asked to log in again).
"""
import base64
import hashlib
import http.server
import json
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import timedelta
from pathlib import Path

from planwell.classify import classify, is_all_day, is_declined
from planwell.timeutil import fmt_clock, get_tz, iso, now_in, parse_iso

REPO_ROOT = Path(__file__).resolve().parent.parent
SECRETS_DIR = REPO_ROOT / "secrets"
CLIENT_FILE = SECRETS_DIR / "google_client.json"
TOKEN_FILE = SECRETS_DIR / "google_tokens.json"
MOCK_CALENDAR = REPO_ROOT / "data" / "mock_calendar.json"

READ_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
WRITE_SCOPE = "https://www.googleapis.com/auth/calendar.events"  # future write-back
SCOPES = [READ_SCOPE]

AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/calendar/v3"

_lock = threading.Lock()


class CalendarNotConnected(Exception):
    """No (usable) Google tokens: run `python3 -m planwell.gcal` once."""


class CalendarError(Exception):
    pass


# ---------------------------------------------------------------- credentials
def load_client(path=CLIENT_FILE):
    path = Path(path)
    if not path.exists():
        raise CalendarNotConnected(
            "Missing %s - download the Desktop-app OAuth client JSON from Google Cloud "
            "Console and save it there." % path)
    data = json.loads(path.read_text())
    c = data.get("installed") or data.get("web") or data
    if not c.get("client_id"):
        raise CalendarNotConnected("%s has no client_id" % path)
    return {"client_id": c["client_id"], "client_secret": c.get("client_secret", ""),
            "auth_uri": c.get("auth_uri") or AUTH_URI, "token_uri": c.get("token_uri") or TOKEN_URI}


def load_tokens(path=TOKEN_FILE):
    path = Path(path)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def save_tokens(tok, path=TOKEN_FILE):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(tok, indent=2))
    try:
        tmp.chmod(0o600)
    except Exception:
        pass
    tmp.replace(path)
    gi = path.parent / ".gitignore"
    if not gi.exists():
        try:
            gi.write_text("*\n")
        except Exception:
            pass


def has_scopes(tok, scopes=None):
    granted = set((tok or {}).get("scope", "").split())
    return all(s in granted for s in (scopes or SCOPES))


def is_connected():
    """True if tokens exist with the needed scopes and a refresh token (no network call)."""
    tok = load_tokens()
    return bool(tok and tok.get("refresh_token") and has_scopes(tok) and CLIENT_FILE.exists())


def _post_form(url, data):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(),
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        raise CalendarError("Token request failed (%s): %s" % (e.code, body[:300]))


def _store_token_response(resp, previous=None):
    tok = dict(previous or {})
    tok["access_token"] = resp["access_token"]
    tok["expires_at"] = int(time.time()) + int(resp.get("expires_in", 3600)) - 60
    if resp.get("refresh_token"):  # Google usually only sends it on first consent
        tok["refresh_token"] = resp["refresh_token"]
    if resp.get("scope"):
        tok["scope"] = resp["scope"]
    tok["token_type"] = resp.get("token_type", "Bearer")
    save_tokens(tok)
    return tok


def refresh(tok=None):
    tok = tok or load_tokens()
    if not tok or not tok.get("refresh_token"):
        raise CalendarNotConnected("Google Calendar is not connected yet.")
    c = load_client()
    try:
        resp = _post_form(c["token_uri"], {
            "client_id": c["client_id"], "client_secret": c["client_secret"],
            "refresh_token": tok["refresh_token"], "grant_type": "refresh_token"})
    except CalendarError as e:
        if "invalid_grant" in str(e):
            raise CalendarNotConnected("Google login expired - run `python3 -m planwell.gcal` again.")
        raise
    return _store_token_response(resp, tok)


def access_token():
    with _lock:
        tok = load_tokens()
        if not tok or not tok.get("refresh_token"):
            raise CalendarNotConnected("Google Calendar is not connected yet.")
        if not has_scopes(tok):
            raise CalendarNotConnected("Google Calendar needs a new login for updated permissions.")
        if not tok.get("access_token") or time.time() >= tok.get("expires_at", 0):
            tok = refresh(tok)
        return tok["access_token"]


# ---------------------------------------------------------------- login flow
def login(scopes=None, open_browser=True, timeout=300):
    """Interactive installed-app OAuth with loopback redirect + PKCE. Returns the token dict."""
    scopes = scopes or SCOPES
    c = load_client()
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(24)
    result = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if "code" not in q and "error" not in q:
                self.send_response(404)
                self.end_headers()
                return
            if q.get("state", [""])[0] != state:
                result["error"] = "state mismatch"
            elif "error" in q:
                result["error"] = q["error"][0]
            else:
                result["code"] = q["code"][0]
            ok = "code" in result
            msg = ("PlanWell is connected to Google Calendar. You can close this tab."
                   if ok else "Google login did not complete (%s). You can close this tab "
                   "and try again." % result.get("error"))
            body = ("<!doctype html><meta charset=utf-8><title>PlanWell</title>"
                    "<body style='font:20px system-ui;margin:3em;line-height:1.5'>%s</body>" % msg)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode())

        def log_message(self, *a):
            pass

    httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    port = httpd.server_address[1]
    redirect_uri = "http://127.0.0.1:%d/" % port
    url = c["auth_uri"] + "?" + urllib.parse.urlencode({
        "client_id": c["client_id"], "redirect_uri": redirect_uri, "response_type": "code",
        "scope": " ".join(scopes), "state": state, "code_challenge": challenge,
        "code_challenge_method": "S256", "access_type": "offline", "prompt": "consent",
    })
    print("Opening your browser to sign in to Google...\nIf it does not open, visit:\n" + url)
    if open_browser:
        webbrowser.open(url)
    httpd.timeout = 1
    deadline = time.time() + timeout
    while not result and time.time() < deadline:
        httpd.handle_request()
    httpd.server_close()
    if "code" not in result:
        raise CalendarError("Google login failed: %s" % result.get("error", "timed out"))
    resp = _post_form(c["token_uri"], {
        "client_id": c["client_id"], "client_secret": c["client_secret"], "code": result["code"],
        "code_verifier": verifier, "grant_type": "authorization_code", "redirect_uri": redirect_uri})
    if not resp.get("scope"):
        resp["scope"] = " ".join(scopes)
    return _store_token_response(resp, load_tokens() or {})


# ---------------------------------------------------------------- events
def _api_get(path, params):
    url = API + path + "?" + urllib.parse.urlencode(params)
    for attempt in range(2):
        req = urllib.request.Request(url, headers={"Authorization": "Bearer " + access_token()})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 401 and attempt == 0:
                with _lock:
                    refresh()
                continue
            body = e.read().decode("utf-8", "replace")
            raise CalendarError("Google Calendar error %s: %s" % (e.code, body[:300]))
    raise CalendarError("Google Calendar request failed")


def normalize_event(raw, tz=None, calendar_id=None):
    """Google event resource -> {id,title,start,end,location} (+ all_day, declined, calendar_id)."""
    st, en = raw.get("start") or {}, raw.get("end") or {}
    all_day = "dateTime" not in st
    s = parse_iso(st.get("dateTime") or st.get("date"), tz)
    e = parse_iso(en.get("dateTime") or en.get("date"), tz)
    return {
        "id": raw.get("id"), "title": raw.get("summary") or "(untitled)",
        "start": iso(s.astimezone(tz)) if s and tz else iso(s),
        "end": iso(e.astimezone(tz)) if e and tz else iso(e),
        "location": raw.get("location") or "",
        "all_day": all_day, "declined": is_declined(raw), "calendar_id": calendar_id,
    }


def resolve_calendar_ids(wanted):
    """Config entries may be calendar ids ("primary", "abc@group.calendar.google.com")
    or calendar names ("Workout"). Names match case-insensitively, exact first, then partial."""
    try:
        cals = _api_get("/users/me/calendarList", {}).get("items", [])
    except CalendarError:
        return list(wanted)
    ids = {c["id"] for c in cals}
    out = []
    for w in wanted:
        if w == "primary" or w in ids:
            out.append(w)
            continue
        wl = w.strip().lower()
        hit = [c["id"] for c in cals if (c.get("summary") or "").strip().lower() == wl] or \
              [c["id"] for c in cals if wl in (c.get("summary") or "").lower()]
        if hit:
            out.extend(hit)
        else:
            print("PlanWell: no calendar named %r found; skipping it" % w, file=sys.stderr)
    seen = set()
    return [x for x in out if not (x in seen or seen.add(x))]


def list_events(start, end, config):
    """Timed, non-declined events overlapping [start, end) from every config google_calendar_ids.

    Returns [{id, title, start, end, location, all_day, declined, calendar_id}] sorted by start.
    Raises CalendarNotConnected if no login yet.
    """
    tz = get_tz(config)
    start, end = parse_iso(start, tz), parse_iso(end, tz)
    out = []
    for cal_id in resolve_calendar_ids((config or {}).get("google_calendar_ids") or ["primary"]):
        token = None
        while True:
            params = {"timeMin": iso(start), "timeMax": iso(end), "singleEvents": "true",
                      "orderBy": "startTime", "maxResults": 250}
            if token:
                params["pageToken"] = token
            data = _api_get("/calendars/%s/events" % urllib.parse.quote(cal_id, safe=""), params)
            for raw in data.get("items", []):
                if raw.get("status") == "cancelled":
                    continue
                ev = normalize_event(raw, tz, cal_id)
                if ev["all_day"] or ev["declined"]:
                    continue
                out.append(ev)
            token = data.get("nextPageToken")
            if not token:
                break
    out.sort(key=lambda e: parse_iso(e["start"], tz))
    return out


def load_mock_events(config, path=MOCK_CALENDAR):
    """Read data/mock_calendar.json (list, {"events": [...]}, or {"items": [...]})."""
    tz = get_tz(config)
    path = Path(path)
    if not path.exists():
        return []
    data = json.loads(path.read_text())
    items = data if isinstance(data, list) else (data.get("events") or data.get("items") or [])
    out = []
    for raw in items:
        if not isinstance(raw, dict):
            continue
        if isinstance(raw.get("start"), dict):  # Google-shaped
            ev = normalize_event(raw, tz)
        else:
            s, e = parse_iso(raw.get("start"), tz), parse_iso(raw.get("end"), tz)
            if s and not e and raw.get("duration_min"):
                e = s + timedelta(minutes=int(raw["duration_min"]))
            ev = dict(raw)
            ev.update({"id": raw.get("id") or "mock-%s" % iso(s), "title": raw.get("title") or raw.get("summary") or "",
                       "start": iso(s.astimezone(tz)) if s else None,
                       "end": iso(e.astimezone(tz)) if e else None,
                       "location": raw.get("location") or ""})
            ev.setdefault("all_day", is_all_day(raw))
            ev.setdefault("declined", is_declined(raw))
        if ev.get("start") and ev.get("end") and not ev["all_day"] and not ev["declined"]:
            out.append(ev)
    return out


def get_events(start, end, config, mock=False):
    """Events overlapping [start, end): mock calendar file in mock mode, Google otherwise."""
    tz = get_tz(config)
    start, end = parse_iso(start, tz), parse_iso(end, tz)
    if not mock:
        return list_events(start, end, config)
    out = [e for e in load_mock_events(config)
           if parse_iso(e["start"], tz) < end and parse_iso(e["end"], tz) > start]
    out.sort(key=lambda e: parse_iso(e["start"], tz))
    return out


# ---------------------------------------------------------------- local history (calendar_events)
def save_history(events, start, end, db_path=None, calendar_ids=None):
    """Upsert events into calendar_events and drop rows in [start, end) of those calendars that are gone.

    Returns the number of events stored.
    """
    from planwell import db
    conn = db.connect(db_path)
    try:
        rows = [{"id": e["id"], "calendar_id": e.get("calendar_id"), "title": e.get("title") or "",
                 "start": e["start"], "end": e["end"], "location": e.get("location") or "",
                 "raw_json": json.dumps(e, separators=(",", ":"))}
                for e in events if e.get("id") and e.get("start") and e.get("end")]
        db.upsert_many(conn, "calendar_events", rows)
        keep = {r["id"] for r in rows}
        cals = set(calendar_ids or []) | {r["calendar_id"] for r in rows if r["calendar_id"]}
        if cals:
            for r in conn.execute('SELECT id, calendar_id, start FROM calendar_events').fetchall():
                t = parse_iso(r["start"])
                if (r["calendar_id"] in cals and r["id"] not in keep and t is not None
                        and parse_iso(start) <= t < parse_iso(end)):
                    conn.execute("DELETE FROM calendar_events WHERE id=?", (r["id"],))
        conn.commit()
        return len(rows)
    finally:
        conn.close()


def pull_history(days_back, days_ahead, config, db_path=None, now=None):
    """Fetch [now - days_back, now + days_ahead] from every configured calendar into calendar_events."""
    now = now_in(config, now)
    start, end = now - timedelta(days=days_back), now + timedelta(days=days_ahead)
    events = list_events(start, end, config)
    return save_history(events, start, end, db_path,
                        calendar_ids=[e.get("calendar_id") for e in events if e.get("calendar_id")])


def main(argv=None):
    import argparse
    from planwell.settings import load_config
    ap = argparse.ArgumentParser(description="Connect Google Calendar and list upcoming activities.")
    ap.add_argument("--mock", action="store_true", help="use data/mock_calendar.json, no login")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--relogin", action="store_true", help="force a fresh Google login")
    ap.add_argument("--history", type=int, metavar="DAYS",
                    help="store the past DAYS (and next 3 days) of events in data/planwell.db calendar_events")
    args = ap.parse_args(argv)
    config = load_config()
    if not args.mock and (args.relogin or not is_connected()):
        try:
            login(open_browser=not args.no_browser)
        except (CalendarNotConnected, CalendarError) as e:
            print(str(e))
            return 1
        print("Saved %s" % TOKEN_FILE)
    now = now_in(config)
    if args.history:
        if args.mock:
            print("--history needs a real Google login (mock calendar events come from mock_data).")
            return 1
        try:
            n = pull_history(args.history, 3, config)
        except (CalendarNotConnected, CalendarError) as e:
            print(str(e))
            return 1
        print("Stored %d calendar events from the last %d days (+ next 3) in calendar_events." % (n, args.history))
        return 0
    try:
        events = get_events(now, now + timedelta(days=7), config, mock=args.mock)
    except (CalendarNotConnected, CalendarError) as e:
        print(str(e))
        return 1
    found = [(e, classify(e, config)) for e in events]
    found = [(e, a) for e, a in found if a]
    print("\nTracked activities in the next 7 days: %d" % len(found))
    tz = get_tz(config)
    for e, a in found:
        s = parse_iso(e["start"], tz).astimezone(tz)
        print("  %s %-9s %-8s %s" % (s.strftime("%a %b %d"), fmt_clock(s), a, e["title"]))
    if not found:
        print("  (none - keywords: %s)" % json.dumps({k: a["keywords"] for k, a in config["activities"].items()}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
