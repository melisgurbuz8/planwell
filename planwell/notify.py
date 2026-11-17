"""Phone notifications via ntfy: topic/token secrets, masking, publishing, detail levels.

Secrets never live in config.json (it may be published):
    secrets/ntfy.json        {"topic": "..."}   (0600; the topic works like a password)
    secrets/ntfy_token.txt   optional access token for reserved/private topics (or env NTFY_TOKEN)

Detail levels (config notify.ntfy_detail):
    minimal  - a nudge only: no activity, time or verdict
    summary  - activity, time and the verdict WORD only (default), e.g.
               "Padel 7:00 pm — Go gently. Open PlanWell for details."  No body numbers, no reasons.
    full     - the same text the Mac shows (headline, top reason, top option)
"""
import base64
import json
import os
import re
import tempfile
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SECRETS_DIR = REPO_ROOT / "secrets"
TOPIC_FILE = "ntfy.json"
TOKEN_FILE = "ntfy_token.txt"
DEFAULT_SERVER = "https://ntfy.sh"
CLICK_URL = "http://127.0.0.1:8765/"
DETAILS = ("minimal", "summary", "full")
DEFAULT_DETAIL = "summary"
MASK = "••••"
TOPIC_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
TAGS = {"green": "green_circle", "yellow": "yellow_circle", "red": "red_circle"}
SUMMARY_WORD = {"green": "Looks good", "yellow": "Take it easier", "red": "Go gently"}
MINIMAL_TITLE = "PlanWell"
MINIMAL_BODY = "You have a new PlanWell check. Open PlanWell on your Mac to see it."


# ------------------------------------------------------------------ secrets
def _dir(secrets_dir=None):
    return Path(secrets_dir) if secrets_dir else SECRETS_DIR


def valid_topic(t):
    return isinstance(t, str) and bool(TOPIC_RE.match(t))


def is_masked(t):
    return isinstance(t, str) and t.startswith(MASK)


def mask_topic(t):
    """'abcd...wxyz' -> '••••wxyz' (empty -> '')."""
    return (MASK + t[-4:]) if t else ""


def load_topic(secrets_dir=None):
    try:
        data = json.loads((_dir(secrets_dir) / TOPIC_FILE).read_text(encoding="utf-8"))
        t = data.get("topic") if isinstance(data, dict) else None
        return t if valid_topic(t) else None
    except Exception:
        return None


def _write_private(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(str(path.parent), 0o700)
    except OSError:
        pass
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp.")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, str(path))
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    gi = path.parent / ".gitignore"
    if not gi.exists():
        try:
            gi.write_text("*\n")
        except OSError:
            pass


def save_topic(topic, secrets_dir=None):
    if not valid_topic(topic):
        raise ValueError("notify.ntfy_topic: use 16-64 letters, digits, - or _ (it works like a password).")
    _write_private(_dir(secrets_dir) / TOPIC_FILE, json.dumps({"topic": topic}) + "\n")


def clear_topic(secrets_dir=None):
    p = _dir(secrets_dir) / TOPIC_FILE
    try:
        p.unlink()
    except OSError:
        pass


def load_token(secrets_dir=None):
    """Access token from env NTFY_TOKEN or secrets/ntfy_token.txt (never from config.json)."""
    t = (os.environ.get("NTFY_TOKEN") or "").strip()
    if t:
        return t
    try:
        t = (_dir(secrets_dir) / TOKEN_FILE).read_text(encoding="utf-8").strip()
        return t or None
    except Exception:
        return None


def topic_for(config, secrets_dir=None):
    """Topic to publish to: secrets/ntfy.json, or (back-compat) a real topic still in the config dict."""
    t = ((config or {}).get("notify") or {}).get("ntfy_topic")
    if valid_topic(t):
        return t
    return load_topic(secrets_dir)


def detail_for(config):
    d = ((config or {}).get("notify") or {}).get("ntfy_detail") or DEFAULT_DETAIL
    return d if d in DETAILS else DEFAULT_DETAIL


def server_for(config):
    return ((config or {}).get("notify") or {}).get("ntfy_server") or DEFAULT_SERVER


# ------------------------------------------------------------------ publishing
def header_safe(s):
    """HTTP headers must be latin-1; use RFC 2047 encoded-word (ntfy supports it) otherwise."""
    try:
        s.encode("latin-1")
        return s
    except UnicodeEncodeError:
        return "=?UTF-8?B?%s?=" % base64.b64encode(s.encode("utf-8")).decode("ascii")


def build_request(topic, title, body, verdict=None, server=DEFAULT_SERVER, at=None, click=CLICK_URL,
                  token=None):
    """urllib Request for one ntfy publish. `at` = unix timestamp for scheduled delivery."""
    if not server.startswith("https://"):
        raise ValueError("ntfy server must use https")
    url = server.rstrip("/") + "/" + topic.strip("/")
    headers = {
        "Title": header_safe(title),
        "Priority": "high" if verdict == "red" else "default",
        "Tags": TAGS.get(verdict, "information_source"),
    }
    if click:
        headers["Click"] = click
    if at is not None:
        headers["At"] = str(int(at))
    if token:
        headers["Authorization"] = "Bearer " + token
    return urllib.request.Request(url, data=body.encode("utf-8"), method="POST", headers=headers)


def publish(topic, title, body, verdict=None, server=DEFAULT_SERVER, at=None, click=CLICK_URL,
            token=None, opener=None, log=None):
    """POST to ntfy. Returns True on success; never raises."""
    if token is None:
        token = load_token()
    try:
        req = build_request(topic, title, body, verdict, server, at, click, token)
        with (opener or urllib.request.urlopen)(req, timeout=20) as r:
            r.read()
        return True
    except Exception as e:
        if log:
            log("ntfy notification failed: %s" % e)
        return False


# ------------------------------------------------------------------ text by detail level
def summary_line(label, clock, verdict):
    return "%s %s — %s" % (label, clock, SUMMARY_WORD.get(verdict, "Check in"))


def render(detail, full_title, full_body, summary_title=None, summary_body=None, verdict=None):
    """(title, body, verdict_for_tags) for the chosen detail level.

    Without a summary (e.g. an unknown payload) "summary" falls back to the minimal nudge.
    Minimal never carries a verdict (no coloured tag / priority that could leak the state).
    """
    if detail == "full":
        return full_title, full_body, verdict
    if detail == "summary" and summary_body:
        return summary_title or MINIMAL_TITLE, summary_body, verdict
    return MINIMAL_TITLE, MINIMAL_BODY, None
