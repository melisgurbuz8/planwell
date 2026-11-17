"""
PlanWell - Step 1: connect your Oura account (one-time login).

Run:  python3 oura_connect.py
- Asks for your Client ID + Client Secret (from developer.ouraring.com)
- Opens your browser to log in to Oura and click "Allow"
- Saves tokens to secrets/oura_tokens.json (never share or commit this folder)
- Tests the connection by listing your recent walking / pilates workouts

Uses only the Python standard library - nothing to install.
"""
import getpass
import http.server
import json
import os
import secrets
import time
import urllib.parse
import urllib.request
import webbrowser
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

AUTHORIZE_URL = "https://cloud.ouraring.com/oauth/authorize"
TOKEN_URL = "https://api.ouraring.com/oauth/token"
API = "https://api.ouraring.com/v2/usercollection"
REDIRECT_URI = "http://localhost:8080/callback"   # must match the portal EXACTLY
SCOPES = "email personal daily heartrate workout session tag spo2 stress heart_health"

HERE = Path(__file__).resolve().parent
SECRETS_DIR = HERE / "secrets"
CONFIG_FILE = SECRETS_DIR / "oura_app.json"
TOKEN_FILE = SECRETS_DIR / "oura_tokens.json"


def load_or_ask_app_credentials():
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text())
    print("Paste the values from your app at https://developer.ouraring.com\n")
    cfg = {
        "client_id": input("Client ID: ").strip(),
        "client_secret": getpass.getpass("Client Secret (hidden as you type): ").strip(),
    }
    SECRETS_DIR.mkdir(exist_ok=True)
    os.chmod(SECRETS_DIR, 0o700)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))
    os.chmod(CONFIG_FILE, 0o600)
    (SECRETS_DIR / ".gitignore").write_text("*\n")
    return cfg


def post_form(url, data):
    req = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(data).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def save_tokens(tok):
    tok["obtained_at"] = int(time.time())
    TOKEN_FILE.write_text(json.dumps(tok, indent=2))
    os.chmod(TOKEN_FILE, 0o600)


def login_in_browser(cfg):
    state = secrets.token_urlsafe(16)
    result = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            result.update({k: v[0] for k, v in q.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            msg = "Connected! You can close this tab." if "code" in q else "Something went wrong - check the terminal."
            self.wfile.write(f"<h2 style='font-family:sans-serif'>{msg}</h2>".encode())

        def log_message(self, *a):
            pass

    url = AUTHORIZE_URL + "?" + urllib.parse.urlencode({
        "response_type": "code",
        "client_id": cfg["client_id"],
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "state": state,
    })
    print("\nOpening your browser to log in to Oura...")
    print("If it doesn't open, paste this link into your browser:\n" + url + "\n")
    webbrowser.open(url)

    server = http.server.HTTPServer(("localhost", 8080), Handler)
    while "code" not in result and "error" not in result:
        server.handle_request()
    server.server_close()

    if "error" in result:
        raise SystemExit(f"Oura returned an error: {result}")
    if result.get("state") != state:
        raise SystemExit("Security check failed (state mismatch). Please run again.")

    tok = post_form(TOKEN_URL, {
        "grant_type": "authorization_code",
        "code": result["code"],
        "redirect_uri": REDIRECT_URI,
        "client_id": cfg["client_id"],
        "client_secret": cfg["client_secret"],
    })
    save_tokens(tok)
    return tok


def refresh_if_needed(cfg, tok):
    """Oura refresh tokens are single-use: always save the new one."""
    age = time.time() - tok.get("obtained_at", 0)
    if age < tok.get("expires_in", 0) - 3600:
        return tok
    new = post_form(TOKEN_URL, {
        "grant_type": "refresh_token",
        "refresh_token": tok["refresh_token"],
        "client_id": cfg["client_id"],
        "client_secret": cfg["client_secret"],
    })
    save_tokens(new)
    return new


def get(tok, path, **params):
    req = urllib.request.Request(
        f"{API}/{path}?{urllib.parse.urlencode(params)}",
        headers={"Authorization": f"Bearer {tok['access_token']}"},
    )
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def main():
    cfg = load_or_ask_app_credentials()
    tok = json.loads(TOKEN_FILE.read_text()) if TOKEN_FILE.exists() else login_in_browser(cfg)
    tok = refresh_if_needed(cfg, tok)

    start = (date.today() - timedelta(days=365)).isoformat()
    workouts, next_token = [], None
    while True:
        params = {"start_date": start, "end_date": date.today().isoformat()}
        if next_token:
            params["next_token"] = next_token
        page = get(tok, "workout", **params)
        workouts += page.get("data", [])
        next_token = page.get("next_token")
        if not next_token:
            break

    print(f"\n✅ Connected to Oura. Found {len(workouts)} workouts in the last 12 months:")
    for activity, n in Counter(w.get("activity") for w in workouts).most_common():
        print(f"   {activity:<20} {n}")
    print("\nNext: send these numbers to Claude and we'll build the history pull.")


if __name__ == "__main__":
    main()
