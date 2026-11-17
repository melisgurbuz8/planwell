"""Load / validate / persist config.json (shared by server and watcher)."""
import copy
import json
import os
import re
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config.json"

# Activity registry (SPEC Addendum v3). Keys are lowercase slugs.
DEFAULT_ACTIVITIES = {
    "walking": {"label": "Walk", "kind": "exercise", "keywords": ["walk", "stroll", "hike"],
                "oura_types": ["walking", "hiking"],
                "traits": {"posture": "standing", "pattern": "steady", "setting": "either"},
                "late_cancel_hours": None},
    "pilates": {"label": "Solidcore", "kind": "exercise", "keywords": ["solidcore", "pilates", "reformer"],
                "oura_types": ["pilates"],
                "traits": {"posture": "mixed", "pattern": "bursty", "setting": "indoor"},
                "late_cancel_hours": None},
    "padel": {"label": "Padel", "kind": "exercise", "keywords": ["padel"],
              "oura_types": ["padel", "paddle_tennis", "tennis", "racquet_sports"],
              "traits": {"posture": "standing", "pattern": "bursty", "setting": "either"},
              "late_cancel_hours": None},
    "class": {"label": "Class", "kind": "daily", "keywords": ["class", "lecture", "seminar", "ba7", "msba"],
              "oura_types": [], "traits": {"posture": "seated", "pattern": "still", "setting": "indoor"},
              "late_cancel_hours": None},
}
KINDS = ("exercise", "daily")
TRAIT_VALUES = {
    "posture": ("seated", "standing", "mixed", "floor"),
    "pattern": ("still", "steady", "bursty"),
    "setting": ("indoor", "outdoor", "either"),
}
DEFAULT_TRAITS = {"posture": "standing", "pattern": "steady", "setting": "either"}
# legacy keys (v1/v2) derived from `activities` on load; still accepted on input for back-compat
LEGACY_KEYS = ("activity_keywords", "activity_labels", "late_cancel_hours")
LABELER_PROVIDERS = ("none", "anthropic", "vertex")
_SLUG = re.compile(r"^[a-z][a-z0-9_]{0,31}$")

DEFAULT_CONFIG = {
    "hr_threshold_bpm": 125,
    "zones_bpm": [100, 115, 125, 140],
    "caution_style": "balanced",
    "precheck_minutes": 60,
    # the ntfy topic is a secret: it lives in secrets/ntfy.json, never in config.json
    "notify": {"macos": True, "ntfy_detail": "summary", "ntfy_server": "https://ntfy.sh"},
    "google_calendar_ids": ["primary", "Workout"],
    "timezone": "America/New_York",
    "preview": {"enabled": True, "time": "20:00"},
    "morning": {"enabled": True, "time": "07:00"},
    "labeler": {"provider": "none", "model": "claude-haiku-5-5", "vertex_project": "your-gcp-project",
                "vertex_region": "us-east5"},
    "activities": copy.deepcopy(DEFAULT_ACTIVITIES),
}

CAUTION_STYLES = ("cautious", "balanced", "flexible")


def _merge(base, extra):
    out = copy.deepcopy(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


# ------------------------------------------------------------------ activity registry
def _norm_activity_def(key, a):
    """Fill missing fields of one activity definition (does not validate)."""
    a = dict(a or {})
    base = DEFAULT_ACTIVITIES.get(key, {})
    out = {
        "label": a.get("label") or base.get("label") or key.replace("_", " ").capitalize(),
        "kind": a.get("kind") or base.get("kind") or "exercise",
        "keywords": list(a["keywords"]) if isinstance(a.get("keywords"), list) else list(base.get("keywords", [key])),
        "oura_types": list(a["oura_types"]) if isinstance(a.get("oura_types"), list) else list(
            base.get("oura_types", [key] if (a.get("kind") or "exercise") == "exercise" else [])),
        "traits": _merge(base.get("traits", DEFAULT_TRAITS), a.get("traits") or {}),
        "late_cancel_hours": a.get("late_cancel_hours", base.get("late_cancel_hours")),
    }
    for k in TRAIT_VALUES:
        out["traits"].setdefault(k, DEFAULT_TRAITS[k])
    for k, v in a.items():  # keep additive fields (e.g. "booked")
        out.setdefault(k, v)
    return out


def derive_legacy(activities):
    """{activity_keywords, activity_labels, late_cancel_hours} views of the registry."""
    return {
        "activity_keywords": {k: list(a["keywords"]) for k, a in activities.items()},
        "activity_labels": {k: a["label"] for k, a in activities.items()},
        "late_cancel_hours": {k: a.get("late_cancel_hours") for k, a in activities.items()},
    }


def _apply_legacy(activities, cfg):
    """Overlay legacy activity_keywords / activity_labels / late_cancel_hours onto the registry."""
    for k, words in (cfg.get("activity_keywords") or {}).items():
        if k in activities and isinstance(words, list):
            activities[k]["keywords"] = list(words)
    for k, lab in (cfg.get("activity_labels") or {}).items():
        if k in activities and isinstance(lab, str) and lab.strip():
            activities[k]["label"] = lab
    for k, h in (cfg.get("late_cancel_hours") or {}).items():
        if k in activities:
            activities[k]["late_cancel_hours"] = h
    return activities


def registry(config):
    """Ordered {key: activity definition} for any config dict (new `activities` or legacy keys)."""
    config = config or {}
    acts = config.get("activities")
    if isinstance(acts, dict) and acts:
        out = {k: _norm_activity_def(k, v) for k, v in acts.items() if isinstance(v, dict)}
    else:
        out = {k: _norm_activity_def(k, v) for k, v in DEFAULT_ACTIVITIES.items()}
        for k in (config.get("activity_keywords") or {}):  # legacy-only extra activities
            if k not in out and _SLUG.match(str(k)):
                out[k] = _norm_activity_def(k, {"keywords": config["activity_keywords"][k]})
    return _apply_legacy(out, config)


def _with_derived(cfg):
    cfg.update(derive_legacy(cfg["activities"]))
    return cfg


def load_config(path=None):
    """config.json merged over defaults (missing/broken file -> defaults).

    Old configs (activity_keywords / activity_labels / late_cancel_hours) are migrated into
    `activities`; the legacy keys are then re-derived from `activities` so older readers keep working.
    """
    path = Path(path) if path else CONFIG_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    data = _migrate_topic(data, path)
    rest = {k: v for k, v in data.items() if k != "activities" and k not in LEGACY_KEYS}
    cfg = _merge({k: v for k, v in DEFAULT_CONFIG.items() if k != "activities"}, rest)
    cfg["activities"] = registry(data)
    _strip_secrets(cfg)
    cfg["notify"]["ntfy_configured"] = bool(_notify().load_topic(secrets_dir_for(path)))
    return _with_derived(cfg)


def _notify():
    from planwell import notify
    return notify


def secrets_dir_for(config_path=None):
    """secrets/ next to config.json (repo root for the real config)."""
    return (Path(config_path) if config_path else CONFIG_PATH).parent / "secrets"


def _strip_secrets(cfg):
    n = cfg.get("notify")
    if isinstance(n, dict):
        n.pop("ntfy_topic", None)
        n.pop("ntfy_configured", None)
    cfg.pop("ntfy_configured", None)
    return cfg


def _migrate_topic(data, path):
    """Move a topic still sitting in config.json into secrets/ntfy.json, then drop it from the file."""
    n = data.get("notify") if isinstance(data.get("notify"), dict) else {}
    t = n.get("ntfy_topic")
    if not t:
        return data
    nt = _notify()
    try:
        if nt.valid_topic(t) and not nt.load_topic(secrets_dir_for(path)):
            nt.save_topic(t, secrets_dir_for(path))
        data = dict(data)
        data["notify"] = {k: v for k, v in n.items() if k not in ("ntfy_topic", "ntfy_configured")}
        if path.exists():
            save_config(data, path)
    except Exception:
        pass  # never fail a load over the migration; the next load retries
    return data


def public_config(cfg, config_path=None):
    """Config for the API: topic masked (••••abcd) + ntfy_configured; never the real topic."""
    out = copy.deepcopy(cfg)
    topic = _notify().load_topic(secrets_dir_for(config_path))
    out.setdefault("notify", {})
    out["notify"]["ntfy_topic"] = _notify().mask_topic(topic) if topic else ""
    out["notify"]["ntfy_configured"] = bool(topic)
    out["ntfy_configured"] = bool(topic)
    return out


def parse_hhmm(value):
    """'20:00' -> (20, 0); None if invalid."""
    try:
        h, m = str(value).strip().split(":")
        h, m = int(h), int(m)
    except (ValueError, AttributeError):
        return None
    if 0 <= h <= 23 and 0 <= m <= 59:
        return h, m
    return None


def validate_partial(partial):
    """Raise ValueError with a friendly message if a partial settings update is invalid."""
    if not isinstance(partial, dict):
        raise ValueError("Settings must be a JSON object.")
    if "hr_threshold_bpm" in partial:
        v = partial["hr_threshold_bpm"]
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not (60 <= v <= 220):
            raise ValueError("hr_threshold_bpm must be a number between 60 and 220.")
    if "zones_bpm" in partial:
        z = partial["zones_bpm"]
        if (not isinstance(z, list) or len(z) != 4
                or not all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in z)
                or any(z[i] >= z[i + 1] for i in range(3))):
            raise ValueError("zones_bpm must be 4 increasing numbers (upper bounds of zones 1-4).")
    if "caution_style" in partial and partial["caution_style"] not in CAUTION_STYLES:
        raise ValueError("caution_style must be one of: cautious, balanced, flexible.")
    if "precheck_minutes" in partial:
        v = partial["precheck_minutes"]
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not (10 <= v <= 240):
            raise ValueError("precheck_minutes must be between 10 and 240.")
    if "activity_keywords" in partial:
        ak = partial["activity_keywords"]
        if not isinstance(ak, dict) or not all(
                isinstance(v, list) and all(isinstance(s, str) for s in v) for v in ak.values()):
            raise ValueError("activity_keywords must map activity -> list of words.")
    if "notify" in partial:
        n = partial["notify"]
        if not isinstance(n, dict):
            raise ValueError("notify must be an object.")
        if "ntfy_detail" in n and n["ntfy_detail"] not in ("minimal", "summary", "full"):
            raise ValueError("notify.ntfy_detail: must be minimal, summary or full.")
        if "macos" in n and not isinstance(n["macos"], bool):
            raise ValueError("notify.macos: must be true or false.")
        if "ntfy_server" in n and not (isinstance(n["ntfy_server"], str) and n["ntfy_server"].startswith("https://")):
            raise ValueError("notify.ntfy_server: must be an https:// address.")
        t = n.get("ntfy_topic")
        from planwell.notify import is_masked
        if t not in (None, "") and not is_masked(t) and not (isinstance(t, str) and re.fullmatch(r"[A-Za-z0-9_-]{16,64}", t)):
            raise ValueError("notify.ntfy_topic: use 16-64 letters, digits, - or _ (it works like a password).")
    if "google_calendar_ids" in partial:
        g = partial["google_calendar_ids"]
        if not isinstance(g, list) or not all(isinstance(s, str) and s for s in g):
            raise ValueError("google_calendar_ids must be a list of calendar ids.")
    if "activity_labels" in partial:
        al = partial["activity_labels"]
        if not isinstance(al, dict) or not all(isinstance(v, str) and v.strip() and len(v) <= 40
                                               for v in al.values()):
            raise ValueError("activity_labels must map activity -> a short non-empty name.")
    if "preview" in partial:
        pv = partial["preview"]
        if not isinstance(pv, dict):
            raise ValueError("preview must be an object like {\"enabled\": true, \"time\": \"20:00\"}.")
        if "enabled" in pv and not isinstance(pv["enabled"], bool):
            raise ValueError("preview.enabled must be true or false.")
        if "time" in pv and parse_hhmm(pv["time"]) is None:
            raise ValueError("preview.time must be a 24-hour time like \"20:00\".")
    if "late_cancel_hours" in partial:
        lc = partial["late_cancel_hours"]
        if not isinstance(lc, dict) or not all(
                v is None or (isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= 72)
                for v in lc.values()):
            raise ValueError("late_cancel_hours values must be null (unknown) or hours between 0 and 72.")
    if "morning" in partial:
        mv = partial["morning"]
        if not isinstance(mv, dict):
            raise ValueError("morning: must be an object like {\"enabled\": true, \"time\": \"07:00\"}.")
        if "enabled" in mv and not isinstance(mv["enabled"], bool):
            raise ValueError("morning.enabled: must be true or false.")
        if "time" in mv and parse_hhmm(mv["time"]) is None:
            raise ValueError("morning.time: must be a 24-hour time like \"07:00\".")
    if "labeler" in partial:
        lb = partial["labeler"]
        if not isinstance(lb, dict):
            raise ValueError("labeler: must be an object.")
        if "provider" in lb and lb["provider"] not in LABELER_PROVIDERS:
            raise ValueError("labeler.provider: must be one of none, anthropic, vertex.")
        if "model" in lb and not (isinstance(lb["model"], str) and lb["model"].strip()):
            raise ValueError("labeler.model: must be a non-empty model name.")
        for k in ("vertex_project", "vertex_region"):
            if k in lb and not isinstance(lb[k], str):
                raise ValueError("labeler.%s: must be a string." % k)
    if "activities" in partial:
        acts = partial["activities"]
        if not isinstance(acts, dict):
            raise ValueError("activities: must map an activity key to its settings (or null to remove it).")
        for k, v in acts.items():
            if not _SLUG.match(str(k)):
                raise ValueError("activities.%s: key must be a short lowercase slug like \"padel\"." % k)
            if v is None:
                continue
            if not isinstance(v, dict):
                raise ValueError("activities.%s: must be an object, or null to remove it." % k)
            _raise_first(_activity_errors(k, v, only_given=True))
    if "timezone" in partial:
        from planwell.timeutil import ZoneInfo
        try:
            if ZoneInfo is not None:
                ZoneInfo(partial["timezone"])
        except Exception:
            raise ValueError("Unknown timezone: %s" % partial["timezone"])


def _activity_errors(key, a, only_given=False):
    """[(field_path, message)] for one activity; only_given=True checks just the fields present."""
    errs = []
    path = "activities.%s." % key
    has = (lambda f: f in a) if only_given else (lambda f: True)
    if has("label"):
        v = a.get("label")
        if not isinstance(v, str) or not v.strip() or len(v) > 40:
            errs.append((path + "label", "needs a short name (1\u201340 characters)."))
    if has("kind") and a.get("kind") not in KINDS:
        errs.append((path + "kind", "must be \"exercise\" or \"daily\"."))
    if has("keywords"):
        kw = a.get("keywords")
        if not isinstance(kw, list) or not kw or not all(isinstance(w, str) and w.strip() for w in kw):
            errs.append((path + "keywords", "needs at least one calendar keyword (non-empty words)."))
    if has("oura_types"):
        ot = a.get("oura_types")
        if not isinstance(ot, list) or not all(isinstance(w, str) and w.strip() for w in ot):
            errs.append((path + "oura_types", "must be a list of Oura activity names."))
    if has("traits"):
        tr = a.get("traits")
        if not isinstance(tr, dict):
            errs.append((path + "traits", "must be an object with posture, pattern and setting."))
        else:
            for t, allowed in TRAIT_VALUES.items():
                if (t in tr or not only_given) and tr.get(t) not in allowed:
                    errs.append((path + "traits." + t, "must be one of %s." % ", ".join(allowed)))
    if has("late_cancel_hours"):
        h = a.get("late_cancel_hours")
        if h is not None and (not isinstance(h, (int, float)) or isinstance(h, bool) or not (0 <= h <= 72)):
            errs.append((path + "late_cancel_hours", "must be null (unknown) or hours between 0 and 72."))
    if "booked" in a and not isinstance(a["booked"], bool):
        errs.append((path + "booked", "must be true or false."))
    return errs


def _raise_first(errs):
    if errs:
        field, msg = errs[0]
        raise ValueError("%s: %s" % (field, msg))


def validate_activity(key, a):
    """Raise ValueError("activities.<key>.<field>: ...") if one (normalised) definition is invalid."""
    _raise_first(_activity_errors(key, a))


def validate_registry(activities):
    if not isinstance(activities, dict) or not activities:
        raise ValueError("activities: keep at least one activity.")
    for k, a in activities.items():
        if not _SLUG.match(str(k)):
            raise ValueError("activities.%s: key must be a short lowercase slug like \"padel\"." % k)
        validate_activity(k, a)


def save_config(config, path=None):
    """Atomically write config.json (legacy keys are not persisted once `activities` exists)."""
    path = Path(path) if path else CONFIG_PATH
    if isinstance(config.get("activities"), dict):
        config = {k: v for k, v in config.items() if k not in LEGACY_KEYS}
    config = _strip_secrets(copy.deepcopy(config))
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".config.", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
            f.write("\n")
        os.replace(tmp, str(path))
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def update_config(partial, path=None):
    """Validate + merge a partial update into config.json, persist, return the new config.

    Lists (keywords, zones, calendar ids) are replaced, not appended; nested dicts are merged.
    `activities` is merged per activity: {"padel": {...}} adds/edits, {"padel": null} removes.
    """
    validate_partial(partial)
    cfg = load_config(path)
    partial = copy.deepcopy(partial)
    partial.pop("ntfy_configured", None)
    pn = partial.get("notify")
    if isinstance(pn, dict):
        pn.pop("ntfy_configured", None)
        if "ntfy_topic" in pn:
            t = pn.pop("ntfy_topic")
            nt = _notify()
            if t == "":
                nt.clear_topic(secrets_dir_for(path))
            elif t is not None and not nt.is_masked(t):
                nt.save_topic(t, secrets_dir_for(path))  # secrets/ntfy.json, 0600 - never config.json
    rest = {k: v for k, v in partial.items() if k != "activities" and k not in LEGACY_KEYS}
    acts = cfg["activities"]
    cfg = _merge({k: v for k, v in cfg.items() if k != "activities"}, rest)
    # legacy keys first, then `activities` (merge per activity; null removes one)
    acts = _apply_legacy(acts, {k: partial[k] for k in LEGACY_KEYS if k in partial})
    for k, v in (partial.get("activities") or {}).items():
        if v is None:
            acts.pop(k, None)
        else:
            acts[k] = _norm_activity_def(k, _merge(acts.get(k, {}), v))
    validate_registry(acts)
    cfg["activities"] = acts
    save_config(cfg, path)
    _strip_secrets(cfg)
    cfg["notify"]["ntfy_configured"] = bool(_notify().load_topic(secrets_dir_for(path)))
    return _with_derived(cfg)


DEFAULT_CONFIG.update(derive_legacy(DEFAULT_CONFIG["activities"]))
