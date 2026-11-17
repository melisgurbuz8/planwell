"""Calendar-event labels (SPEC "Addendum v3 — AI event labels"): calendar TEXT only.

    label_event(event, config, db_path=None, http=None, token_fn=None) -> LABEL
    label_events(events, config, db_path=None, **kw) -> {event_id: LABEL}
    build_payload(event, config) -> {title, location, duration_min, start_hour, weekday}
    rules_label(payload, config) -> LABEL           (provider "none", and every fallback)
    is_unrecognised(label) -> bool                  ("Track this as …" candidates)

LABEL = {"category": "<activity key or 'other'>", "posture": "seated|standing|mixed|floor",
         "pattern": "still|steady|bursty", "setting": "indoor|outdoor|either", "exertion": 1-5,
         "source": "ai|rules|cache"}

Privacy contract: the ONLY event data that can leave this machine is what build_payload() returns
(title, location, duration in minutes, local start hour, weekday). Never Oura data, never verdicts or
body numbers. The prompt additionally lists the user's activity category names (from settings) so
the model can pick one. Providers: "none" (default, rules only), "anthropic", "vertex". Any
provider failure (network, auth, bad JSON, invalid values) falls back to the rules labeller.
AI results are cached in the `event_labels` table keyed by sha256 of the payload.
"""
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from planwell.timeutil import get_tz, parse_iso

REPO_ROOT = Path(__file__).resolve().parent.parent
ANTHROPIC_KEY_FILE = REPO_ROOT / "secrets" / "anthropic_key.txt"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
VERTEX_URL = ("https://{region}-aiplatform.googleapis.com/v1/projects/{project}/locations/{region}"
              "/publishers/anthropic/models/{model}:rawPredict")
DEFAULT_LABELER = {"provider": "none", "model": "claude-haiku-5-5",
                   "vertex_project": "your-gcp-project", "vertex_region": "us-east5"}
PAYLOAD_KEYS = ("title", "location", "duration_min", "start_hour", "weekday")
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
POSTURES = ("seated", "standing", "mixed", "floor")
PATTERNS = ("still", "steady", "bursty")
SETTINGS = ("indoor", "outdoor", "either")
TIMEOUT = 15
MAX_TEXT = 200

LABELS_SCHEMA = "CREATE TABLE IF NOT EXISTS event_labels(key TEXT PRIMARY KEY, label_json TEXT, created_at TEXT)"

# Defaults for the registry when config.json still has the pre-v3 keys.
DEFAULT_ACTIVITIES = {
    "walking": {"label": "Walk", "kind": "exercise", "keywords": ["walk", "stroll", "hike"],
                "oura_types": ["walking", "hiking"],
                "traits": {"posture": "standing", "pattern": "steady", "setting": "either"}, "late_cancel_hours": None},
    "pilates": {"label": "Solidcore", "kind": "exercise", "keywords": ["solidcore", "pilates", "reformer"],
                "oura_types": ["pilates"],
                "traits": {"posture": "mixed", "pattern": "bursty", "setting": "indoor"}, "late_cancel_hours": None},
    "padel": {"label": "Padel", "kind": "exercise", "keywords": ["padel"],
              "oura_types": ["padel", "paddle_tennis", "tennis", "racquet_sports"],
              "traits": {"posture": "standing", "pattern": "bursty", "setting": "either"}, "late_cancel_hours": None},
    "class": {"label": "Class", "kind": "daily", "keywords": ["class", "lecture", "seminar", "ba7", "msba"],
              "oura_types": [], "traits": {"posture": "seated", "pattern": "still", "setting": "indoor"},
              "late_cancel_hours": None},
}


# ------------------------------------------------------------------ registry
def registry(config):
    """{key: activity spec}: settings.registry() when present, else config['activities'] / pre-v3 keys."""
    config = config or {}
    try:
        from planwell import settings as _settings
        fn = getattr(_settings, "registry", None)
        if callable(fn):
            reg = fn(config)
            if isinstance(reg, dict) and reg:
                return reg
    except Exception:
        pass
    acts = config.get("activities")
    if isinstance(acts, dict) and acts:
        out = {}
        for k, v in acts.items():
            if isinstance(v, dict):
                base = DEFAULT_ACTIVITIES.get(k, {})
                spec = dict(base, **v)
                spec["traits"] = dict(base.get("traits") or {}, **(v.get("traits") or {}))
                out[k] = spec
        if out:
            return out
    out = {}
    kws = config.get("activity_keywords") or {}
    labels = config.get("activity_labels") or {}
    lch = config.get("late_cancel_hours") or {}
    for k in (list(kws) or ["walking", "pilates"]):
        base = DEFAULT_ACTIVITIES.get(k, {"label": k.capitalize(), "kind": "exercise", "oura_types": [k],
                                          "traits": {"posture": "mixed", "pattern": "steady", "setting": "either"}})
        spec = dict(base)
        spec["keywords"] = list(kws.get(k) or base.get("keywords") or [k])
        spec["label"] = labels.get(k) or base.get("label")
        spec["late_cancel_hours"] = lch.get(k)
        out[k] = spec
    for k, spec in DEFAULT_ACTIVITIES.items():  # v3 defaults (padel, class) until settings migrates
        out.setdefault(k, dict(spec))
    return out


def labeler_config(config):
    return dict(DEFAULT_LABELER, **((config or {}).get("labeler") or {}))


# ------------------------------------------------------------------ payload (the whitelist)
def _clean_text(s):
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s[:MAX_TEXT]


def build_payload(event, config):
    """The ONLY function that turns an event into data for a labeller. Whitelisted fields only."""
    tz = get_tz(config)
    title = event.get("title") or event.get("summary") or ""
    loc = event.get("location") or ""
    start = event.get("start")
    end = event.get("end")
    if isinstance(start, dict):  # raw Google shape
        start = start.get("dateTime") or start.get("date")
    if isinstance(end, dict):
        end = end.get("dateTime") or end.get("date")
    s, e = parse_iso(start, tz), parse_iso(end, tz)
    dur = None
    if s and e and e > s:
        dur = int(round((e - s).total_seconds() / 60.0))
    elif event.get("duration_min") is not None:
        try:
            dur = int(round(float(event["duration_min"])))
        except (TypeError, ValueError):
            dur = None
    hour = weekday = None
    if s:
        ls = s.astimezone(tz)
        hour = ls.hour
        weekday = WEEKDAYS[ls.weekday()]
    return {"title": _clean_text(title), "location": _clean_text(loc), "duration_min": dur,
            "start_hour": hour, "weekday": weekday}


def payload_key(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ rules labeller
# (words, posture, pattern, setting, exertion) — first matching group wins; checked on title + location
_RULES = [
    (["hiit", "spin", "spinning", "cycle class", "soulcycle", "bootcamp", "boot camp",
      "crossfit", "f45", "orangetheory", "boxing", "kickboxing", "rowing", "run", "running", "jog", "jogging",
      "race", "5k", "10k"], "standing", "bursty", "indoor", 4),
    (["tennis", "pickleball", "squash", "badminton", "racquetball", "basketball", "soccer", "football",
      "volleyball", "frisbee", "ultimate"], "standing", "bursty", "outdoor", 4),
    (["climbing", "bouldering", "dance", "dancing", "zumba", "salsa", "swim", "swimming", "ski", "skiing",
      "skating", "bike", "biking", "cycling", "gym", "workout", "weights", "lifting", "strength",
      "moving day", "move out", "move in", "moving boxes", "help move"], "mixed", "bursty", "either", 4),
    (["yoga", "bikram", "vinyasa", "barre", "trx"], "mixed", "steady", "indoor", 3),
    (["gardening", "golf", "hiking", "trail", "farmers market", "festival", "concert", "gig", "party",
      "wedding", "museum", "gallery", "zoo", "tour", "volunteer", "shopping", "groceries", "grocery",
      "errands", "costco", "ikea", "cleaning", "chores", "cook", "cooking", "bake"], "standing", "steady", "either", 2),
    (["stretch", "stretching", "physio", "physical therapy", "pt session", "tai chi", "qigong",
      "meditation", "restorative", "yin yoga", "sound bath"], "floor", "still", "indoor", 1),
    (["class", "lecture", "seminar", "lab", "exam", "study", "office hours", "tutorial", "workshop",
      "meeting", "standup", "stand-up", "sync", "1:1", "1on1", "call", "zoom", "teams", "interview",
      "coffee", "lunch", "dinner", "breakfast", "brunch", "drinks", "chat", "movie", "film", "theater",
      "theatre", "doctor", "dentist", "appointment", "haircut", "flight", "train", "drive", "commute",
      "presentation", "webinar", "review"], "seated", "still", "indoor", 1),
]
_OUTDOOR = ["park", "trail", "beach", "outdoor", "outside", "river", "riverside", "field", "lake",
            "garden", "court", "esplanade", "common", "pier"]
_INDOOR = ["studio", "gym", "hall", "room", "library", "office", "zoom", "online", "home", "center", "centre"]


def _has_word(text, words):
    for w in words:
        if re.search(r"(?<![a-z0-9])%s(?![a-z0-9])" % re.escape(w.lower()), text):
            return w
    return None


def _match_activity(text, reg):
    """First (earliest-position) activity keyword in `text` -> key or None."""
    best = None
    for key, spec in reg.items():
        for kw in spec.get("keywords") or []:
            kw = str(kw).strip().lower()
            if not kw:
                continue
            m = re.search(r"(?<![a-z0-9])%s(?:s|es|ing|ed|er|ers)?(?![a-z0-9])" % re.escape(kw).replace(r"\ ", r"\s+"),
                          text)
            if m and (best is None or m.start() < best[0]):
                best = (m.start(), key)
    return best[1] if best else None


def rules_label(payload, config):
    """Keyword + heuristic labeller (no network). Always returns a valid LABEL with source 'rules'."""
    reg = registry(config)
    title = (payload.get("title") or "").lower()
    loc = (payload.get("location") or "").lower()
    text = (title + " | " + loc).strip()
    dur = payload.get("duration_min") or 0
    hour = payload.get("start_hour")
    cat = _match_activity(title, reg) or _match_activity(loc, reg)
    posture, pattern, setting, ex = "seated", "still", "either", 1
    if cat:
        spec = reg[cat]
        tr = spec.get("traits") or {}
        posture = tr.get("posture") or "mixed"
        pattern = tr.get("pattern") or "steady"
        setting = tr.get("setting") or "either"
        if spec.get("kind") == "daily":
            ex = 2 if posture == "standing" else 1
        else:
            ex = {"bursty": 4, "steady": 3, "still": 2}.get(pattern, 3)
    else:
        cat = "other"
        for words, p, pat, st, e in _RULES:
            if _has_word(text, words):
                posture, pattern, setting, ex = p, pat, st, e
                break
        if _has_word(text, ["hot", "heated", "infrared"]) and ex >= 3:
            ex += 1  # heat on top of exertion
            setting = "indoor"
    if setting == "either":
        if _has_word(text, _OUTDOOR):
            setting = "outdoor"
        elif _has_word(text, _INDOOR):
            setting = "indoor"
    if ex >= 3 and dur >= 90:
        ex += 1
    if ex >= 2 and dur and dur <= 20:
        ex -= 1
    if ex >= 3 and hour is not None and 11 <= hour <= 16 and setting == "outdoor":
        ex += 1  # midday outdoors
    return {"category": cat, "posture": posture, "pattern": pattern, "setting": setting,
            "exertion": max(1, min(5, int(ex))), "source": "rules"}


def is_unrecognised(label):
    """Not one of the tracked activities, but effortful enough to suggest 'Track this as …'."""
    return bool(label) and label.get("category") == "other" and int(label.get("exertion") or 0) >= 3


def suggest_activity(label, config):
    """Best existing activity to offer for an unrecognised event (same posture/pattern), or None."""
    if not label:
        return None
    best = None
    for key, spec in registry(config).items():
        tr = spec.get("traits") or {}
        score = (tr.get("pattern") == label.get("pattern")) * 2 + (tr.get("posture") == label.get("posture"))
        if spec.get("kind") == "exercise" and score and (best is None or score > best[0]):
            best = (score, key)
    return best[1] if best else None


# ------------------------------------------------------------------ AI providers
SYSTEM_PROMPT = (
    "You label personal calendar events for an activity planner. You only see calendar text. "
    "Reply with ONE JSON object and nothing else, with exactly these keys: "
    '"category" (one of the given category keys, or "other"), '
    '"posture" ("seated"|"standing"|"mixed"|"floor"), "pattern" ("still"|"steady"|"bursty"), '
    '"setting" ("indoor"|"outdoor"|"either"), "exertion" (integer 1-5: 1 = sitting still, '
    "3 = a moderate workout, 5 = very strenuous).")


def build_prompt(payload, config):
    """User message: the whitelisted payload + category names from settings. Nothing else."""
    cats = {k: {"label": v.get("label"), "kind": v.get("kind")} for k, v in registry(config).items()}
    return ("Categories: %s\nEvent: %s\nJSON only."
            % (json.dumps(cats, ensure_ascii=False, sort_keys=True),
               json.dumps({k: payload.get(k) for k in PAYLOAD_KEYS}, ensure_ascii=False, sort_keys=True)))


def _messages_body(payload, config, model=None):
    body = {"max_tokens": 200, "temperature": 0, "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": build_prompt(payload, config)}]}
    if model:
        body["model"] = model
    return body


def _default_http(url, headers, body, timeout=TIMEOUT):
    """POST bytes -> (status, bytes). Replaced by a fake in tests."""
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read() if hasattr(e, "read") else b""


def _anthropic_key():
    k = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if k:
        return k
    try:
        return ANTHROPIC_KEY_FILE.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _gcloud_token():
    try:
        r = subprocess.run(["gcloud", "auth", "print-access-token"], stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    tok = r.stdout.decode("utf-8", "replace").strip()
    return tok if r.returncode == 0 and tok else None


def build_request(payload, config, token_fn=None):
    """(url, headers, body_bytes) for the configured provider, or None if not usable."""
    lc = labeler_config(config)
    provider = (lc.get("provider") or "none").lower()
    model = lc.get("model") or DEFAULT_LABELER["model"]
    if provider == "anthropic":
        key = _anthropic_key()
        if not key:
            return None
        body = _messages_body(payload, config, model)
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
        return ANTHROPIC_URL, headers, json.dumps(body).encode("utf-8")
    if provider == "vertex":
        tok = (token_fn or _gcloud_token)()
        if not tok:
            return None
        region = lc.get("vertex_region") or DEFAULT_LABELER["vertex_region"]
        url = VERTEX_URL.format(region=region, project=lc.get("vertex_project") or DEFAULT_LABELER["vertex_project"],
                                model=model)
        body = _messages_body(payload, config)
        body["anthropic_version"] = "vertex-2023-10-16"
        headers = {"Authorization": "Bearer %s" % tok, "content-type": "application/json"}
        return url, headers, json.dumps(body).encode("utf-8")
    return None


def _extract_json(text):
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except ValueError:
        return None


def validate_label(obj, config):
    """Strictly validated LABEL (without source) or None."""
    if not isinstance(obj, dict):
        return None
    cats = set(registry(config)) | {"other"}
    cat = obj.get("category")
    ex = obj.get("exertion")
    if isinstance(ex, float) and ex == int(ex):
        ex = int(ex)
    if (cat not in cats or obj.get("posture") not in POSTURES or obj.get("pattern") not in PATTERNS
            or obj.get("setting") not in SETTINGS or not isinstance(ex, int) or isinstance(ex, bool)
            or not 1 <= ex <= 5):
        return None
    return {"category": cat, "posture": obj["posture"], "pattern": obj["pattern"],
            "setting": obj["setting"], "exertion": ex}


def ai_label(payload, config, http=None, token_fn=None):
    """Ask the configured provider. Returns a LABEL (source 'ai') or None on any failure."""
    req = build_request(payload, config, token_fn)
    if req is None:
        return None
    url, headers, body = req
    try:
        status, raw = (http or _default_http)(url, headers, body, TIMEOUT)
        if status != 200:
            return None
        data = json.loads(raw.decode("utf-8"))
        text = "".join(c.get("text", "") for c in (data.get("content") or []) if isinstance(c, dict))
    except Exception:
        return None
    lab = validate_label(_extract_json(text), config)
    if lab is None:
        return None
    lab["source"] = "ai"
    return lab


# ------------------------------------------------------------------ cache
def _cache_conn(db_path):
    conn = sqlite3.connect(str(db_path), timeout=5)
    conn.execute(LABELS_SCHEMA)
    return conn


def cache_get(db_path, key, config):
    if not db_path:
        return None
    try:
        conn = _cache_conn(db_path)
        try:
            row = conn.execute("SELECT label_json FROM event_labels WHERE key=?", (key,)).fetchone()
        finally:
            conn.close()
        lab = validate_label(json.loads(row[0]), config) if row else None
    except (sqlite3.Error, ValueError, TypeError):
        return None
    if lab:
        lab["source"] = "cache"
    return lab


def cache_put(db_path, key, label):
    if not db_path:
        return
    try:
        conn = _cache_conn(db_path)
        try:
            conn.execute("INSERT OR REPLACE INTO event_labels(key, label_json, created_at) VALUES (?,?,?)",
                         (key, json.dumps({k: v for k, v in label.items() if k != "source"}),
                          datetime.now(timezone.utc).replace(microsecond=0).isoformat()))
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        pass


# ------------------------------------------------------------------ public API
def label_event(event, config, db_path=None, http=None, token_fn=None):
    """LABEL for one calendar event. Provider 'none' (default) -> rules; AI failures -> rules."""
    payload = build_payload(event, config)
    provider = (labeler_config(config).get("provider") or "none").lower()
    if provider in ("anthropic", "vertex"):
        key = payload_key(payload)
        hit = cache_get(db_path, key, config)
        if hit:
            return hit
        lab = ai_label(payload, config, http=http, token_fn=token_fn)
        if lab:
            cache_put(db_path, key, lab)
            return lab
    return rules_label(payload, config)


def label_events(events, config, db_path=None, **kw):
    """{event_id: LABEL} for many events (each labelled once per distinct payload)."""
    out, memo = {}, {}
    for ev in events or []:
        payload = build_payload(ev, config)
        k = payload_key(payload)
        if k not in memo:
            memo[k] = label_event(ev, config, db_path=db_path, **kw)
        out[ev.get("id")] = dict(memo[k])
    return out
