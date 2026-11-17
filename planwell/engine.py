"""PlanWell engine: session metrics, day features, similar-days matching, verdicts and options.

Public API (shapes match SPEC "JSON API"):
    build_today(db_path, config, events, now=None, mode=None, calendar_connected=None) -> dict
    build_tomorrow(db_path, config, events, now=None) -> /api/tomorrow dict
    build_next48(db_path, config, events, now=None) -> /api/next48 dict
    check(db_path, config, activity, start, duration_min, now=None, event=None) -> CHECK dict
    sessions(db_path, config, activity=None, limit=60) -> {"sessions": [...], "zones_bpm", "hr_threshold_bpm"}
    session_detail(db_path, config, workout_id) -> {"session": {...}, "hr": [{t, bpm}]} | None

Everything tolerates missing data: absent tables/days/heart-rate simply produce None values and
the verdict falls back to simple rules. This module never makes medical claims; wording is
"may"/"looks like", supportive, and always leaves the choice with the person.
"""
import math
import os
import sqlite3
import statistics
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from planwell.classify import classify
from planwell.settings import registry
from planwell.timeutil import add_days, fmt_clock, fmt_hm, get_tz, iso, now_in, parse_iso, utc_str

try:
    from planwell.db import connect as _db_connect
except Exception:  # pragma: no cover - during development before db.py exists
    _db_connect = None

BODY_FALLBACK_KEYS = ("readiness", "readiness_baseline", "sleep_score", "total_sleep_min", "hrv",
                      "hrv_baseline", "hrv_delta_pct", "rhr", "rhr_baseline", "rhr_delta",
                      "temperature_deviation")
ACTIVITY_ALIASES = {
    "walking": {"walking", "walk", "hiking", "hike", "stroll"},
    "pilates": {"pilates", "barre", "reformer", "mat_pilates", "solidcore"},
}
DEFAULT_LABELS = {"walking": "Walk", "pilates": "Solidcore"}
# order = least disruptive first (on red, "go" is moved last)
OPTION_ORDER = ["go", "modify", "buffer", "shorten", "swap", "move", "rebook"]

# (feature, weight, minimum std used for standardisation)
FEATURES = [
    ("readiness", 1.5, 5.0),
    ("hrv_delta_pct", 1.5, 6.0),
    ("rhr_delta", 1.0, 2.0),
    ("sleep_score", 1.0, 5.0),
    ("total_sleep_min", 0.75, 30.0),
    ("stress_before", 0.4, 20.0),
    ("steps_before", 0.4, 1500.0),
    ("temperature_deviation", 0.5, 0.2),
    ("hour_of_day", 0.6, 1.5),
    ("duration_min", 0.75, 8.0),
    ("load_yesterday", 0.3, 5.0),
]
DAILY_MIN_COVERAGE = 0.3  # daily-kind (calendar) sessions need this much heart-rate coverage
COLD_START_K = 5.0        # own-history weight w = n / (n + COLD_START_K)
COMMON_NOUNS = {"walk", "padel", "class", "yoga", "run", "swim", "hike", "tennis", "stretch", "ride", "cycle",
                "spin", "gym", "lecture", "seminar", "dance", "climb", "row", "stroll", "workout", "lift"}

STYLES = {
    # r1/r2: similar-days ratio cut-offs; yellow/red: total caution points needed
    # d1/d2: minimum extra minutes-above-threshold per 30 min (vs typical) for those cut-offs
    "cautious": {"r1": 1.15, "r2": 1.6, "d1": 1.0, "d2": 3.0, "yellow": 1, "red": 3},
    "balanced": {"r1": 1.3, "r2": 1.9, "d1": 1.5, "d2": 5.0, "yellow": 2, "red": 4},
    "flexible": {"r1": 1.5, "r2": 2.4, "d1": 2.5, "d2": 7.0, "yellow": 3, "red": 5},
}
MIN_SESSIONS = 5
BASELINE_DAYS = 30
POST_WINDOW_MIN = 30
GAP_FILL_MIN = 5
STALE_MINUTES = 6 * 60
TIME_BUCKETS = [("morning", 5, 11), ("midday", 11, 15), ("afternoon", 15, 18), ("evening", 18, 24)]

_STORES = {}
_STORES_LOCK = threading.RLock()


# =============================================================== helpers
def norm_activity(a, config=None):
    """Registry key for an activity name / Oura type. Without config: legacy aliases only."""
    a = (a or "").strip().lower()
    if config is not None:
        reg = registry(config)
        if a in reg:
            return a
        for key, d in reg.items():
            if a in [t.lower() for t in d.get("oura_types") or []]:
                return key
    for key, aliases in ACTIVITY_ALIASES.items():
        if a == key or a in aliases:
            return key
    return a


def activity_def(activity, config):
    """Registry definition (falls back to a generic exercise definition)."""
    d = registry(config).get(activity)
    if d:
        return d
    return {"label": DEFAULT_LABELS.get(activity) or (activity or "activity").replace("_", " ").capitalize(),
            "kind": "exercise", "keywords": [], "oura_types": [],
            "traits": {"posture": "standing", "pattern": "steady", "setting": "either"}, "late_cancel_hours": None}


def label_for(activity, config=None):
    """Display label for an activity key from the registry, e.g. pilates -> "Solidcore"."""
    return activity_def(norm_activity(activity, config), config)["label"]


def word_for(activity, config=None):
    """Label as used mid-sentence: common nouns lower-case ("walk", "padel", "class"), names kept ("Solidcore")."""
    lab = label_for(activity, config)
    return lab.lower() if lab.lower() in COMMON_NOUNS else lab


def kind_of(activity, config):
    return activity_def(activity, config).get("kind", "exercise")


def _trait_weight(a, b):
    """How much activity b can stand in for a: shared posture/pattern -> 1.0, one shared -> 0.5."""
    ta, tb = a.get("traits") or {}, b.get("traits") or {}
    shared = sum(1 for t in ("posture", "pattern") if ta.get(t) and ta.get(t) == tb.get(t))
    return {2: 1.0, 1: 0.5}.get(shared, 0.0)


def _median(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _wmedian(pairs):
    """Weighted median of [(weight, value)]."""
    pairs = sorted((v, w) for w, v in pairs if v is not None and w > 0)
    if not pairs:
        return None
    total = sum(w for _v, w in pairs)
    acc = 0.0
    for v, w in pairs:
        acc += w
        if acc >= total / 2.0:
            return v
    return pairs[-1][0]


def _r(x, nd=0):
    if x is None:
        return None
    if nd == 0:
        return int(round(x))
    return round(float(x), nd)


def _connect(path):
    if _db_connect is not None:
        conn = _db_connect(path)
    else:
        conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def _rows(conn, sql, args=()):
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    except sqlite3.Error:
        return []


def _db_signature(path):
    sig = []
    for p in (str(path), str(path) + "-wal"):
        try:
            st = os.stat(p)
            sig.append((st.st_mtime_ns, st.st_size))
        except OSError:
            sig.append(None)
    return tuple(sig)


def _waking_fraction(hour):
    """Rough fraction of the waking day (7:00-23:00) elapsed at `hour` (float)."""
    if hour is None:
        return 1.0
    return min(1.0, max(0.0, (hour - 7.0) / 16.0))


def _local_hour(dt, tz):
    d = dt.astimezone(tz)
    return d.hour + d.minute / 60.0


# =============================================================== store
class Store(object):
    """In-memory snapshot of the daily tables + workout list + session-metrics cache."""

    def __init__(self, path):
        self.path = Path(path)
        self.signature = None
        self.metrics = {}  # (workout_id, threshold, zones) -> metrics
        self.lock = threading.RLock()
        self._feature_cache = {}
        self._reset()

    def _reset(self):
        self.workouts, self.by_id = [], {}
        self.cal_events, self.cal_by_id = [], {}
        self.readiness, self.sleep_score, self.long_sleep = {}, {}, {}
        self.stress, self.activity = {}, {}
        self.last_sync, self.latest_hr = None, None
        self.hr_utc = True
        self._feature_cache = {}

    def refresh(self):
        sig = _db_signature(self.path)
        if sig == self.signature:
            return self
        with self.lock:
            if sig == self.signature:
                return self
            old_by_id = dict(self.by_id)
            old_by_id.update(self.cal_by_id)
            self._reset()
            if self.path.exists():
                conn = _connect(self.path)
                try:
                    self._load(conn)
                finally:
                    conn.close()
            # keep metrics only for workouts that are long finished (their HR won't change)
            cutoff = datetime.now(timezone.utc) - timedelta(days=2)
            keep = {}
            for key, m in self.metrics.items():
                w = self.by_id.get(key[0]) or self.cal_by_id.get(key[0])
                ow = old_by_id.get(key[0])
                if w and ow and w["end"] and w["end"] < cutoff and w["end"] == ow["end"]:
                    keep[key] = m
            self.metrics = keep
            self.signature = _db_signature(self.path)
        return self

    def _load(self, conn):
        for r in _rows(conn, "SELECT * FROM workouts"):
            s, e = parse_iso(r.get("start_datetime")), parse_iso(r.get("end_datetime"))
            if not s or not e or e <= s:
                continue
            w = {"id": r["id"], "activity": norm_activity(r.get("activity")),
                 "raw_activity": r.get("activity"), "day": r.get("day") or s.date().isoformat(),
                 "start": s, "end": e, "label": r.get("label"), "intensity": r.get("intensity")}
            self.workouts.append(w)
            self.by_id[w["id"]] = w
        self.workouts.sort(key=lambda w: w["start"])
        for r in _rows(conn, 'SELECT id, calendar_id, title, start, "end", location FROM calendar_events'):
            s, e = parse_iso(r.get("start")), parse_iso(r.get("end"))
            if not s or not e or e <= s or not r.get("id"):
                continue
            ev = {"id": "cal-%s" % r["id"], "event_id": r["id"], "calendar_id": r.get("calendar_id"),
                  "title": r.get("title") or "", "location": r.get("location") or "",
                  "start": s, "end": e, "raw_activity": None, "label": r.get("title")}
            self.cal_events.append(ev)
            self.cal_by_id[ev["id"]] = ev
        self.cal_events.sort(key=lambda w: w["start"])
        for r in _rows(conn, "SELECT day, score, temperature_deviation FROM daily_readiness"):
            self.readiness[r["day"]] = r
        for r in _rows(conn, "SELECT day, score FROM daily_sleep"):
            self.sleep_score[r["day"]] = r["score"]
        for r in _rows(conn, "SELECT day, type, total_sleep_duration, average_hrv, lowest_heart_rate "
                             "FROM sleep"):
            if (r.get("type") or "long_sleep") != "long_sleep":
                continue
            prev = self.long_sleep.get(r["day"])
            if prev is None or (r.get("total_sleep_duration") or 0) > (prev.get("total_sleep_duration") or 0):
                self.long_sleep[r["day"]] = r
        for r in _rows(conn, "SELECT day, stress_high, recovery_high, day_summary FROM daily_stress"):
            self.stress[r["day"]] = r
        for r in _rows(conn, "SELECT day, steps FROM daily_activity"):
            self.activity[r["day"]] = r.get("steps")
        syncs = [parse_iso(r.get("last_synced_at")) for r in _rows(conn, "SELECT last_synced_at FROM sync_state")]
        syncs = [s for s in syncs if s]
        self.last_sync = max(syncs) if syncs else None
        last = _rows(conn, "SELECT timestamp FROM heartrate ORDER BY timestamp DESC LIMIT 50")
        stamps = [parse_iso(r["timestamp"]) for r in last]
        stamps = [s for s in stamps if s]
        self.latest_hr = max(stamps) if stamps else None
        if last:
            t = last[0]["timestamp"] or ""
            self.hr_utc = t.endswith("+00:00") or t.endswith("Z")

    # ----------------------------------------------------------- heart rate
    def hr_between(self, start, end, conn=None):
        """[(datetime, bpm)] for start <= t <= end, sorted."""
        margin = timedelta(minutes=1) if self.hr_utc else timedelta(hours=15)
        lo, hi = utc_str(start - margin), utc_str(end + margin + timedelta(seconds=1))
        own = conn is None
        if own:
            if not self.path.exists():
                return []
            conn = _connect(self.path)
        try:
            rows = conn.execute("SELECT timestamp, bpm FROM heartrate WHERE timestamp >= ? AND timestamp < ? "
                                "ORDER BY timestamp", (lo, hi)).fetchall()
        except sqlite3.Error:
            rows = []
        finally:
            if own:
                conn.close()
        out = []
        for ts, bpm in rows:
            t = parse_iso(ts)
            if t is not None and bpm is not None and start <= t <= end:
                out.append((t, bpm))
        out.sort(key=lambda x: x[0])
        return out

    # ----------------------------------------------------------- metrics
    def metrics_for(self, w, threshold, zones, conn=None):
        key = (w["id"], threshold, tuple(zones))
        m = self.metrics.get(key)
        if m is None:
            samples = self.hr_between(w["start"], w["end"] + timedelta(minutes=POST_WINDOW_MIN), conn)
            m = compute_metrics(samples, w["start"], w["end"], threshold, zones)
            with self.lock:
                self.metrics[key] = m
        return m

    def all_metrics(self, workouts, threshold, zones):
        missing = [w for w in workouts if (w["id"], threshold, tuple(zones)) not in self.metrics]
        if missing and self.path.exists():
            conn = _connect(self.path)
            try:
                for w in missing:
                    self.metrics_for(w, threshold, zones, conn)
            finally:
                conn.close()
        return [self.metrics_for(w, threshold, zones) for w in workouts]

    # ----------------------------------------------------------- day features
    def _baseline(self, day, getter):
        vals = []
        for i in range(1, BASELINE_DAYS + 1):
            v = getter(add_days(day, -i))
            if v is not None:
                vals.append(v)
        return _median(vals) if len(vals) >= 3 else None

    def day_features(self, day, hour=None, partial=False):
        """Features for `day`. partial=True means 'today so far' (steps/stress not scaled)."""
        key = (day, None if partial else (round(hour, 2) if hour is not None else None), partial)
        if key in self._feature_cache:
            return dict(self._feature_cache[key])
        rd = self.readiness.get(day) or {}
        ls = self.long_sleep.get(day) or {}
        hrv = ls.get("average_hrv")
        rhr = ls.get("lowest_heart_rate")
        tsd = ls.get("total_sleep_duration")
        hrv_base = self._baseline(day, lambda d: (self.long_sleep.get(d) or {}).get("average_hrv"))
        rhr_base = self._baseline(day, lambda d: (self.long_sleep.get(d) or {}).get("lowest_heart_rate"))
        rd_base = self._baseline(day, lambda d: (self.readiness.get(d) or {}).get("score"))
        st = self.stress.get(day) or {}
        stress_min = st.get("stress_high")
        stress_min = stress_min / 60.0 if stress_min is not None else None  # Oura: seconds
        steps = self.activity.get(day)
        frac = 1.0 if partial else _waking_fraction(hour)
        f = {
            "readiness": rd.get("score"),
            "readiness_baseline": rd_base,
            "sleep_score": self.sleep_score.get(day),
            "total_sleep_min": tsd / 60.0 if tsd is not None else None,
            "hrv": hrv, "hrv_baseline": hrv_base,
            "hrv_delta_pct": ((hrv - hrv_base) / hrv_base * 100.0) if (hrv is not None and hrv_base) else None,
            "rhr": rhr, "rhr_baseline": rhr_base,
            "rhr_delta": (rhr - rhr_base) if (rhr is not None and rhr_base is not None) else None,
            "stress_high_min": stress_min,
            "stress_before": stress_min * frac if stress_min is not None else None,
            "stress_summary": st.get("day_summary"),
            "steps": steps,
            "steps_before": steps * frac if steps is not None else None,
            "temperature_deviation": rd.get("temperature_deviation"),
            "hour_of_day": hour,
        }
        self._feature_cache[key] = f
        return dict(f)

    def session_source(self, sid):
        return self.by_id.get(sid) or self.cal_by_id.get(sid)

    def has_body_data(self, day):
        return day in self.readiness or day in self.long_sleep or day in self.sleep_score


def get_store(db_path):
    p = str(Path(db_path).resolve())
    with _STORES_LOCK:
        st = _STORES.get(p)
        if st is None:
            st = _STORES[p] = Store(p)
    return st.refresh()


def clear_cache():
    with _STORES_LOCK:
        _STORES.clear()
    with _RECORDS_LOCK:
        _RECORDS_CACHE.clear()


# =============================================================== session metrics
def zone_index(bpm, zones):
    for i, ub in enumerate(zones):
        if bpm < ub:
            return i
    return len(zones)


def compute_metrics(samples, start, end, threshold, zones):
    """Per-session metrics from [(t, bpm)] covering start..end(+post window)."""
    duration = max(1, int(round((end - start).total_seconds() / 60.0)))
    buckets = [[] for _ in range(duration)]
    post = []
    peak_raw = None
    for t, b in samples:
        off = (t - start).total_seconds() / 60.0
        if off < 0:
            continue
        if off < duration:
            buckets[int(off)].append(b)
        else:
            post.append((off - duration, b))
    minute = [sum(b) / len(b) if b else None for b in buckets]
    covered = sum(1 for v in minute if v is not None)
    # fill short gaps with the nearest real minute (handles sparse 5-min sampling)
    filled = list(minute)
    idx = [i for i, v in enumerate(minute) if v is not None]
    if idx:
        j = 0
        for i in range(duration):
            if filled[i] is not None:
                continue
            while j + 1 < len(idx) and abs(idx[j + 1] - i) <= abs(idx[j] - i):
                j += 1
            if abs(idx[j] - i) <= GAP_FILL_MIN:
                filled[i] = minute[idx[j]]
    vals = [v for v in filled if v is not None]
    n_zones = len(zones) + 1
    m = {"duration_min": duration, "hr_coverage": round(covered / float(duration), 2),
         "filled_coverage": round(sum(1 for v in filled if v is not None) / float(duration), 2),
         "avg_hr": None, "peak_hr": None, "minutes_above_threshold": None,
         "minutes_per_zone": [0] * n_zones, "recovery_drop_bpm": None,
         "usable": False, "above_prefix": None}
    if not vals:
        return m
    peak = max(v for v in minute if v is not None)
    above = [1 if (v is not None and v >= threshold) else 0 for v in filled]
    prefix = [0]
    for a in above:
        prefix.append(prefix[-1] + a)
    zcount = [0] * n_zones
    for v in vals:
        zcount[zone_index(v, zones)] += 1
    rec = [b for o, b in post if 8 <= o <= 12]
    if not rec:
        near = sorted((abs(o - 10), b) for o, b in post if 5 <= o <= 15)
        rec = [near[0][1]] if near else []
    m.update({
        "avg_hr": sum(vals) / len(vals), "peak_hr": peak,
        "minutes_above_threshold": prefix[-1], "minutes_per_zone": zcount,
        "recovery_drop_bpm": (peak - statistics.median(rec)) if rec else None,
        "usable": len(vals) >= 0.5 * duration, "above_prefix": prefix,
    })
    return m


def _rate(m):
    """Minutes above threshold per 30 minutes."""
    if not m or not m.get("usable") or m.get("minutes_above_threshold") is None:
        return None
    return m["minutes_above_threshold"] * 30.0 / max(1, m["duration_min"])


# =============================================================== session records
def _load_by_day(recs):
    """{day: total minutes above threshold} over session records."""
    out = {}
    for r in recs:
        v = r["m"].get("minutes_above_threshold")
        if v is not None and r["m"].get("usable"):
            out[r["w"]["day"]] = out.get(r["w"]["day"], 0) + v
    return out


_RECORDS_CACHE = {}
_RECORDS_LOCK = threading.Lock()


def _all_records(store, config):
    """Every tracked session (exercise workouts + daily calendar sessions), oldest first (memoised)."""
    import json as _json
    try:
        key = (str(store.path), store.signature, _json.dumps(
            {k: config.get(k) for k in ("hr_threshold_bpm", "zones_bpm", "activities", "activity_keywords",
                                        "timezone")}, sort_keys=True, default=str))
    except Exception:
        key = None
    if key is not None:
        with _RECORDS_LOCK:
            hit = _RECORDS_CACHE.get(key)
        if hit is not None:
            return list(hit)
    out = _compute_all_records(store, config)
    if key is not None:
        with _RECORDS_LOCK:
            if len(_RECORDS_CACHE) > 16:
                _RECORDS_CACHE.clear()
            _RECORDS_CACHE[key] = out
    return list(out)


def _compute_all_records(store, config):
    thr = config.get("hr_threshold_bpm", 125)
    zones = config.get("zones_bpm", [100, 115, 125, 140])
    tz = get_tz(config)
    reg = registry(config)
    items = []
    for w in store.workouts:
        act = norm_activity(w.get("raw_activity") or w.get("activity"), config)
        if act in reg and reg[act].get("kind") == "exercise":
            items.append(dict(w, activity=act, kind="exercise"))
    for ev in getattr(store, "cal_events", []):
        act = classify({"title": ev["title"], "start": iso(ev["start"])}, config)
        if act and reg[act].get("kind") == "daily":
            items.append(dict(ev, activity=act, kind="daily", day=ev["start"].astimezone(tz).date().isoformat()))
    ms = store.all_metrics(items, thr, zones)
    out = []
    for w, m in zip(items, ms):
        if w["kind"] == "daily" and (m.get("filled_coverage") or 0) < DAILY_MIN_COVERAGE:
            continue  # daytime HR is sparse; skip classes we can't really see
        hour = _local_hour(w["start"], tz)
        f = store.day_features(w["day"], hour)
        f["duration_min"] = m["duration_min"]
        out.append({"w": w, "m": m, "f": f, "rate": _rate(m)})
    out.sort(key=lambda r: r["w"]["start"])
    loads = _load_by_day(out)
    for r in out:
        r["f"]["load_yesterday"] = loads.get(add_days(r["w"]["day"], -1), 0)
    return out


def _session_records(store, config, activity=None, before=None):
    """Session records {"w", "m", "f", "rate"} for one activity (or all tracked), ended by `before`."""
    act = norm_activity(activity, config) if activity else None
    return [r for r in _all_records(store, config)
            if (act is None or r["w"]["activity"] == act) and (before is None or r["w"]["end"] <= before)]


def session_public(rec, tz):
    w, m, f = rec["w"], rec["m"], rec["f"]
    return {
        "workout_id": w["id"], "date": w["day"], "start": iso(w["start"].astimezone(tz)),
        "end": iso(w["end"].astimezone(tz)), "activity": w["activity"],
        "duration_min": m["duration_min"], "avg_hr": _r(m["avg_hr"]), "peak_hr": _r(m["peak_hr"]),
        "minutes_above_threshold": m["minutes_above_threshold"],
        "minutes_per_zone": list(m["minutes_per_zone"]),
        "recovery_drop_bpm": _r(m["recovery_drop_bpm"]),
        "readiness": f.get("readiness"), "hrv_delta_pct": _r(f.get("hrv_delta_pct"), 1),
        "hr_coverage": m["hr_coverage"],
        "title": w.get("title") if w.get("kind") == "daily" else None,
        "kind": w.get("kind", "exercise"),
    }


# =============================================================== similarity
def find_similar(target, pool, k):
    """Weighted, standardised distance over FEATURES. Returns [(similarity, distance, rec)]."""
    sds = {}
    for name, _w, floor in FEATURES:
        vals = [r["f"].get(name) for r in pool if r["f"].get(name) is not None]
        sd = statistics.pstdev(vals) if len(vals) >= 2 else 0.0
        sds[name] = max(sd, floor)
    scored = []
    for r in pool:
        num = den = 0.0
        for name, wt, _floor in FEATURES:
            a, b = target.get(name), r["f"].get(name)
            if a is None or b is None:
                continue
            num += wt * ((a - b) / sds[name]) ** 2
            den += wt
        d = math.sqrt(num / den) if den else 3.0
        scored.append((math.exp(-d * d / 4.0), d, r))
    scored.sort(key=lambda x: (x[1], -x[2]["w"]["start"].timestamp()))
    return scored[:k]


def choose_k(n):
    return max(5, min(8, 4 + n // 10))


# =============================================================== guard rails & reasons
def guard_points(f):
    """Simple body-state guard rails -> (points, [flag names])."""
    pts, flags = 0, []
    rd = f.get("readiness")
    if rd is not None:
        if rd < 50:
            pts += 2; flags.append("readiness")
        elif rd < 60:
            pts += 1; flags.append("readiness")
    hd = f.get("hrv_delta_pct")
    if hd is not None:
        if hd <= -25:
            pts += 2; flags.append("hrv")
        elif hd <= -15:
            pts += 1; flags.append("hrv")
    rr = f.get("rhr_delta")
    if rr is not None:
        if rr >= 7:
            pts += 2; flags.append("rhr")
        elif rr >= 4:
            pts += 1; flags.append("rhr")
    td = f.get("temperature_deviation")
    if td is not None and td >= 0.5:
        pts += 1; flags.append("temp")
    ts = f.get("total_sleep_min")
    if ts is not None and ts < 330:
        pts += 1; flags.append("sleep")
    return min(pts, 4), flags


def _body_reasons(f):
    """[(severity, text)] where severity > 0 is a caution, < 0 reassuring."""
    out = []
    hd = f.get("hrv_delta_pct")
    if hd is not None:
        if hd <= -8:
            out.append((min(3.0, -hd / 10.0), "HRV is %d%% below your usual" % round(-hd)))
        elif hd >= 8:
            out.append((-1.0, "HRV is %d%% above your usual" % round(hd)))
        else:
            out.append((-0.6, "HRV is close to your usual"))
    rr = f.get("rhr_delta")
    if rr is not None:
        if rr >= 3:
            out.append((min(3.0, rr / 3.0), "Resting heart rate is %d bpm above your usual" % round(rr)))
        elif rr <= -2:
            out.append((-0.8, "Resting heart rate is a little lower than usual"))
        else:
            out.append((-0.5, "Resting heart rate is close to your usual"))
    rd = f.get("readiness")
    if rd is not None:
        base = f.get("readiness_baseline")
        if rd < 60 or (base is not None and rd <= base - 8):
            usual = " (usually around %d)" % round(base) if base is not None else ""
            out.append((min(3.0, max(1.0, (70 - rd) / 8.0)), "Readiness is %d%s" % (rd, usual)))
        elif rd >= 75:
            out.append((-0.9, "Readiness is a solid %d" % rd))
        else:
            out.append((-0.3, "Readiness is %d" % rd))
    ts = f.get("total_sleep_min")
    if ts is not None:
        if ts < 360:
            out.append((min(2.5, (420 - ts) / 60.0), "You slept %s" % fmt_hm(ts)))
        elif ts >= 420:
            out.append((-0.7, "You slept %s" % fmt_hm(ts)))
        else:
            out.append((-0.2, "You slept %s" % fmt_hm(ts)))
    td = f.get("temperature_deviation")
    if td is not None and td >= 0.4:
        out.append((1.2, "Body temperature is running a little warm (+%.1f°C)" % td))
    return out


def day_assessment(f, style):
    s = STYLES.get(style, STYLES["balanced"])
    has_any = any(f.get(k) is not None for k in ("readiness", "hrv", "rhr", "sleep_score", "total_sleep_min"))
    if not has_any:
        return "yellow", "Today's Oura data hasn't arrived yet, so PlanWell is being a little extra gentle.", 0
    pts, _flags = guard_points(f)
    if pts >= s["red"] - 1 and pts >= 3:
        return "red", "A rest-leaning day — your body is quite a bit below its usual.", pts
    if pts >= max(1, s["yellow"] - 1):
        return "yellow", "A gentler day — your body is a bit below its usual.", pts
    return "green", "Your body looks close to its usual today.", pts


# =============================================================== options
def _round5(x):
    return int(5 * round(float(x) / 5))


def _option(type_, label, detail, new_start=None, new_end=None):
    return {"type": type_, "label": label, "detail": detail, "new_start": new_start, "new_end": new_end}


def _shorten_option(neigh_pool, duration, typical_rate, threshold, start, tz):
    if duration < 20:
        return None
    target_rate = max(1.0, typical_rate if typical_rate is not None else 1.0)
    cands = list(range(_round5(duration - 5) if _round5(duration - 5) < duration else duration - 5, 9, -5))
    chosen, chosen_avg = None, None
    for n in cands:
        if n >= duration or n < 10:
            continue
        pairs = []
        for sim, _d, r in neigh_pool:
            pre = r["m"].get("above_prefix")
            if pre and r["m"].get("usable"):
                pairs.append((sim, pre[min(n, len(pre) - 1)]))
        if not pairs:
            break
        avg = _wmedian(pairs)
        if avg <= target_rate * n / 30.0:
            chosen, chosen_avg = n, avg
            break
    if chosen is None:
        chosen = max(10, min(duration - 5, _round5(duration / 2.0)))
        detail = "A shorter session leaves room to stop while you still feel okay."
    elif chosen_avg < 0.5:
        detail = "On similar days, the first %d min mostly stayed under %d bpm." % (chosen, threshold)
    else:
        detail = ("On similar days, the first %d min usually had only about %d min above %d bpm."
                  % (chosen, max(1, round(chosen_avg)), threshold))
    new_end = start + timedelta(minutes=chosen)
    return _option("shorten", "Shorten to %d min" % chosen, detail,
                   iso(start.astimezone(tz)), iso(new_end.astimezone(tz)))


def _swap_option(activity, config=None):
    t = (activity_def(activity, config).get("traits") or {}) if config is not None else {}
    if activity == "walking":
        return _option("swap", "Swap to a shorter, flat or indoor walk",
                       "Flat ground and a cool indoor space keep the effort steadier; slow laps at home count too.")
    if activity == "pilates":
        return _option("swap", "Swap to a floor-only mat session",
                       "A mat session at home, staying on the floor with fewer standing transitions, is usually "
                       "gentler on heart rate than a full %s class." % label_for(activity, config))
    if t.get("pattern") == "bursty" and t.get("posture") == "standing":
        return _option("swap", "Swap to a gentler, slower-paced session",
                       "A drill or practice session with longer pauses keeps the bursts shorter than match play.")
    if t.get("posture") in ("mixed", "floor"):
        return _option("swap", "Swap to a floor-based session",
                       "Staying on the floor with fewer standing transitions is usually gentler on heart rate.")
    if t.get("pattern") == "steady" and t.get("posture") == "standing":
        return _option("swap", "Swap to a shorter, flatter or indoor version",
                       "An easier route or a cool indoor space keeps the effort steadier.")
    return _option("swap", "Swap to a gentler version",
                   "A lower-effort version, with more seated or lying-down work, may feel better today.")


def _bucket(hour):
    for name, lo, hi in TIME_BUCKETS:
        if lo <= hour < hi:
            return name, lo, hi
    return None, None, None


def _modify_option(activity, config=None):
    t = (activity_def(activity, config).get("traits") or {}) if config is not None else {}
    if activity == "pilates" or t.get("posture") in ("mixed", "floor"):
        return _option("modify", "Plan floor modifications",
                       "Ask your coach for floor-based options and fewer standing transitions, and take a short "
                       "break between blocks whenever you need one.")
    return _option("modify", "Plan more breaks",
                   "Take a break between games or sets, and keep rallies shorter whenever you need to.")


def _rest_after_option(activity, config=None):
    return _option("buffer", "Add a rest block after",
                   "Block 15\u201320 minutes after your %s to rest seated or lying down before the next thing."
                   % word_for(activity, config))


def _seated_break_option(activity, config=None):
    return _option("modify", "Plan a seated break",
                   "Sit when you can, and plan a seated break partway through \u2014 a seat near the door "
                   "makes stepping out easy.")


def _bucket_groups(recs):
    groups = {}
    for r in recs:
        if r["rate"] is None:
            continue
        b, _lo, _hi = _bucket(r["f"]["hour_of_day"])
        if b:
            groups.setdefault(b, []).append(r)
    return groups


def _rebook_option(recs, start, duration, now, threshold, tz, activity, config=None):
    """Solidcore: suggest a later class when later times of day went calmer, else a generic nudge."""
    hour = _local_hour(start, tz)
    cur, _clo, _chi = _bucket(hour)
    groups = _bucket_groups(recs)
    cur_rates = [r["rate"] for r in groups.get(cur, [])] if cur else []
    best = None
    if len(cur_rates) >= 2:
        cur_mean = sum(cur_rates) / len(cur_rates)
        for name, lo, hi in TIME_BUCKETS:
            if name == cur or lo < hour:
                continue
            g = groups.get(name, [])
            if len(g) < 3:
                continue
            mean = sum(r["rate"] for r in g) / len(g)
            if mean <= cur_mean * 0.75 and cur_mean - mean >= 1.0 and (best is None or mean < best[1]):
                best = (name, mean, cur_mean, _median([r["f"]["hour_of_day"] for r in g]))
    word = word_for(activity, config)
    if best:
        name, mean, cur_mean, typical_hour = best
        scale = duration / 30.0
        local_start = start.astimezone(tz)
        day0 = local_start.replace(hour=0, minute=0, second=0, microsecond=0)
        proposed = day0 + timedelta(minutes=int(round(typical_hour * 60 / 15.0)) * 15)
        if proposed > local_start + timedelta(minutes=30):
            return _option("rebook", "Book a later class (around %s)" % fmt_clock(proposed),
                           "Your %s %s classes have usually stayed calmer (about %d vs %d min above %d bpm)."
                           % (name, word, round(mean * scale), round(cur_mean * scale), threshold),
                           iso(proposed), iso(proposed + timedelta(minutes=duration)))
    return _option("rebook", "Book a later class",
                   "A later %s class may suit, if one fits your day \u2014 it gives your body "
                   "more time to settle." % word)


def _move_option(recs, start, duration, now, threshold, tz, activity, config=None):
    hour = _local_hour(start, tz)
    cur, _clo, _chi = _bucket(hour)
    if cur is None:
        return None
    groups = {}
    for r in recs:
        if r["rate"] is None:
            continue
        b, _lo, _hi = _bucket(r["f"]["hour_of_day"])
        if b:
            groups.setdefault(b, []).append(r)
    cur_rates = [r["rate"] for r in groups.get(cur, [])]
    if len(cur_rates) < 2:
        return None
    cur_med = sum(cur_rates) / len(cur_rates)  # mean: sensitive to the harder days
    best = None
    for name, lo, hi in TIME_BUCKETS:
        if name == cur or lo >= hour:
            continue
        g = groups.get(name, [])
        if len(g) < 3:
            continue
        med = sum(r["rate"] for r in g) / len(g)
        if med <= cur_med * 0.75 and cur_med - med >= 1.0:
            if best is None or med < best[1]:
                best = (name, med, lo, hi, _median([r["f"]["hour_of_day"] for r in g]))
    if not best:
        return None
    name, med, lo, hi, typical_hour = best
    local_start = start.astimezone(tz)
    day0 = local_start.replace(hour=0, minute=0, second=0, microsecond=0)
    proposed = day0 + timedelta(minutes=int(round(typical_hour * 60 / 15.0)) * 15)
    earliest = now.astimezone(tz) + timedelta(minutes=30)
    if proposed < earliest:
        proposed = earliest + timedelta(minutes=(15 - earliest.minute % 15) % 15)
        proposed = proposed.replace(second=0, microsecond=0)
    if proposed.hour + proposed.minute / 60.0 >= hi or proposed >= local_start - timedelta(minutes=30):
        return None
    scale = duration / 30.0
    return _option("move", "Move earlier to %s" % fmt_clock(proposed),
                   "Your %s %s sessions have usually stayed calmer (about %d vs %d min above %d bpm)."
                   % (name, word_for(activity, config), round(med * scale), round(cur_med * scale), threshold),
                   iso(proposed), iso(proposed + timedelta(minutes=duration)))


def _buffer_option():
    return _option("buffer", "Add a 15-min rest buffer",
                   "Rest seated or lying down for 15 minutes before you start, with water nearby.")


def _go_option(verdict, activity, config=None):
    if verdict == "green":
        return _option("go", "Go as planned",
                       "Today looks like days when your %s went well." % (
                           word_for(activity, config) + ("" if kind_of(activity, config) == "daily" else " sessions")))
    return _option("go", "Go as planned",
                   "If you go, it's okay to pause or stop early — adjusting is always a good call.")


# =============================================================== check
HEADLINES = {
    "green": "Looks like a good fit today",
    "yellow": "Take it a little easier today",
    "red": "A gentler plan may feel better today",
}


def _freshness(store, now):
    latest = store.latest_hr
    return {
        "last_sync": iso(store.last_sync.astimezone(now.tzinfo)) if store.last_sync else None,
        "latest_hr_sample": iso(latest.astimezone(now.tzinfo)) if latest else None,
        "minutes_old": int(max(0, (now - latest).total_seconds() // 60)) if latest else None,
    }


def _expect(neigh):
    """(expected rate, expected peak) from [(similarity, distance, rec)]."""
    if not neigh:
        return None, None
    wsum = sum(x[0] for x in neigh)
    if wsum <= 1e-6:
        return (_mean([r["rate"] for _s, _d, r in neigh]), _mean([r["m"]["peak_hr"] for _s, _d, r in neigh]))
    # blend of weighted mean and weighted median: robust to one unusual session
    wmean = sum(sim * r["rate"] for sim, _d, r in neigh) / wsum
    wmed = _wmedian([(sim, r["rate"]) for sim, _d, r in neigh])
    peaks = [(sim, r["m"]["peak_hr"]) for sim, _d, r in neigh if r["m"]["peak_hr"] is not None]
    ps = sum(p[0] for p in peaks)
    return 0.5 * (wmean + wmed), (sum(a * b for a, b in peaks) / ps if ps else None)


def _blend(own, nb, w):
    if own is None:
        return nb
    if nb is None:
        return own
    return w * own + (1.0 - w) * nb


def is_rebookable(adef):
    """Booked sessions: a late-cancel window is set, or an explicit/implied booked class."""
    if adef.get("kind") != "exercise":
        return False
    if adef.get("late_cancel_hours") is not None:
        return True
    if "booked" in adef:
        return bool(adef["booked"])
    return (adef.get("traits") or {}).get("setting") == "indoor"


def build_options(verdict, activity, config, start, duration_min, now, tz, thr,
                  pool_for_shorten=(), typical_rate=None, usable=()):
    """Options by activity kind and traits, least disruptive first; on red "go" goes last."""
    adef = activity_def(activity, config)
    traits = adef.get("traits") or {}
    if adef.get("kind") == "daily":
        options = [_go_option(verdict, activity, config), _rest_after_option(activity, config)]
        if verdict != "green":
            options.append(_seated_break_option(activity, config))
    else:
        options = [_go_option(verdict, activity, config), _buffer_option()]
        if verdict != "green":
            if traits.get("pattern") == "bursty" or traits.get("posture") in ("mixed", "floor"):
                options.append(_modify_option(activity, config))
            sh = _shorten_option(pool_for_shorten, duration_min, typical_rate, thr, start, tz)
            if sh:
                options.append(sh)
            options.append(_swap_option(activity, config))
            if is_rebookable(adef):
                options.append(_rebook_option(list(usable), start, duration_min, now, thr, tz, activity, config))
            else:
                mv = _move_option(list(usable), start, duration_min, now, thr, tz, activity, config)
                if mv:
                    options.append(mv)
    options.sort(key=lambda o: OPTION_ORDER.index(o["type"]) if o["type"] in OPTION_ORDER else 99)
    if verdict == "red":  # lowest-disruption gentle options first; "go" stays available, last
        options = [o for o in options if o["type"] != "go"] + [o for o in options if o["type"] == "go"]
    return options


def _check(store, config, activity, start, duration_min, now, event=None):
    tz = get_tz(config)
    thr = config.get("hr_threshold_bpm", 125)
    style = config.get("caution_style", "balanced")
    s = STYLES.get(style, STYLES["balanced"])
    activity = norm_activity(activity, config)
    adef = activity_def(activity, config)
    akind = adef.get("kind", "exercise")
    start = parse_iso(start, tz) if not isinstance(start, datetime) else start
    if start.tzinfo is None:
        start = start.replace(tzinfo=tz)
    duration_min = max(5, int(round(float(duration_min or 30))))
    end = start + timedelta(minutes=duration_min)
    if event and event.get("end"):
        e = parse_iso(event["end"], tz)
        if e and e > start:
            end = e
            duration_min = int(round((end - start).total_seconds() / 60))

    today = now.astimezone(tz).date().isoformat()
    feat_day = min(start.astimezone(tz).date().isoformat(), today)
    target = store.day_features(feat_day, partial=(feat_day == today))
    # Early morning the ring often hasn't synced last night yet. Don't fall back to history alone:
    # borrow body signals from the most recent day that has them, and say so.
    body_from = None
    if not store.has_body_data(feat_day):
        for back in (1, 2):
            prev = add_days(feat_day, -back)
            if store.has_body_data(prev):
                pf = store.day_features(prev)
                for k in BODY_FALLBACK_KEYS:
                    target[k] = pf.get(k)
                body_from = prev
                break
    target["hour_of_day"] = _local_hour(start, tz)
    target["duration_min"] = float(duration_min)

    all_recs = _session_records(store, config, None, before=now)
    loads = _load_by_day(all_recs)
    target["load_yesterday"] = loads.get(add_days(start.astimezone(tz).date().isoformat(), -1), 0)
    usable = [r for r in all_recs if r["rate"] is not None and r["w"]["activity"] == activity]
    n = len(usable)
    scale = duration_min / 30.0

    # ---- own-history expectation
    own_exp, own_peak, own_typ, own_neigh, pool_for_shorten = None, None, None, [], []
    if n:
        k = choose_k(n) if n >= MIN_SESSIONS else n
        own_neigh = find_similar(target, usable, k)
        pool_for_shorten = find_similar(target, usable, min(n, 2 * k))
        own_exp, own_peak = _expect(own_neigh)
        own_typ = _median([r["rate"] for r in usable])

    # ---- cold-start / trait neighbours: activities sharing posture/pattern
    nb_exp = nb_peak = nb_typ = None
    nb_neigh = []
    nb_weights = {}
    for key, d in registry(config).items():
        if key != activity:
            w_ = _trait_weight(adef, d)
            if w_ > 0:
                nb_weights[key] = w_
    nb_pool = [r for r in all_recs if r["rate"] is not None and r["w"]["activity"] in nb_weights]
    if len(nb_pool) >= MIN_SESSIONS:
        nb_neigh = find_similar(target, nb_pool, choose_k(len(nb_pool)))
        nb_neigh = [(sim * nb_weights[r["w"]["activity"]], d_, r) for sim, d_, r in nb_neigh]
        nb_exp, nb_peak = _expect(nb_neigh)
        nb_typ = _wmedian([(nb_weights[r["w"]["activity"]], r["rate"]) for r in nb_pool])
    if nb_exp is None:
        w_own = 1.0 if n else 0.0
    else:
        w_own = n / (n + COLD_START_K)
    expected_rate = _blend(own_exp, nb_exp, w_own)
    expected_peak = _blend(own_peak, nb_peak, w_own)
    typical_rate = _blend(own_typ, nb_typ, w_own)
    using_neighbours = nb_exp is not None and n < MIN_SESSIONS

    gpts, gflags = guard_points(target)
    sim_pts = 0
    if expected_rate is not None and (n >= MIN_SESSIONS or nb_exp is not None):
        ratio = (expected_rate + 2.0) / ((typical_rate or 0.0) + 2.0)
        diff = expected_rate - (typical_rate or 0.0)
        if ratio >= s["r2"] and diff >= s["d2"]:
            sim_pts = 3
        elif ratio >= s["r1"] and diff >= s["d1"]:
            sim_pts = 2
        elif ratio <= 0.8:
            sim_pts = -1
        if expected_rate >= 12:
            sim_pts += 1
        if using_neighbours:  # borrowed history counts for less
            sim_pts = int(round(sim_pts * (0.5 + 0.5 * w_own)))
    points = sim_pts + gpts

    if points >= s["red"]:
        verdict = "red"
    elif points >= s["yellow"]:
        verdict = "yellow"
    else:
        verdict = "green"

    neighbours = own_neigh[:choose_k(max(n, MIN_SESSIONS))] if n >= MIN_SESSIONS else \
        (own_neigh + sorted(nb_neigh, key=lambda x: -x[0]))[:MIN_SESSIONS]

    has_today = any(target.get(k) is not None for k in ("readiness", "hrv", "rhr"))
    mean_sim = _mean([x[0] for x in neighbours]) if neighbours else None
    if n < MIN_SESSIONS:
        confidence = "learning"
    elif n >= 15 and has_today and (mean_sim or 0) >= 0.45:
        confidence = "good"
    else:
        confidence = "moderate"

    # ---- reasons
    act_word = word_for(activity, config)
    body = _body_reasons(target)
    reasons = []
    sim_reason = None
    if expected_rate is not None and (n >= MIN_SESSIONS or using_neighbours):
        exp_m, typ_m = round(expected_rate * scale), round((typical_rate or 0) * scale)
        who = ("your %s sessions" % act_word) if not using_neighbours else "your similar activities"
        if exp_m <= typ_m:
            sim_reason = ("On similar days, %s stayed about as calm as usual (~%d min above %d bpm)"
                          % (who, exp_m, thr))
        else:
            sim_reason = ("On similar days, %s spent about %d min above %d bpm (usually %d)"
                          % (who, exp_m, thr, typ_m))
    if verdict == "green":
        body.sort(key=lambda x: x[0])
        if sim_reason:
            reasons.append(sim_reason)
        reasons += [t for sev, t in body if sev <= 0][:3]
        reasons += [t for sev, t in body if sev > 0][:1]
    else:
        body.sort(key=lambda x: -x[0])
        cautions = [t for sev, t in body if sev > 0]
        reasons += cautions[:2]
        if sim_reason:
            reasons.append(sim_reason)
        reasons += cautions[2:3]
        if len(reasons) < 2:
            reasons += [t for sev, t in body if sev <= 0][: 2 - len(reasons)]
    yl = loads.get(add_days(start.astimezone(tz).date().isoformat(), -1), 0)
    if verdict != "green" and yl and yl >= 10:
        reasons.append("Yesterday had about %d min above %d bpm across your sessions" % (yl, thr))
    if n < MIN_SESSIONS:
        if using_neighbours:
            reasons.insert(0, "Still learning %s \u2014 using your similar activities for now" % act_word)
        else:
            if n == 0:
                reasons.insert(0, "Still learning \u2014 no past %s sessions with heart-rate data yet, "
                                  "so this uses simple rules" % act_word)
            else:
                reasons.insert(0, "Still learning \u2014 only %d past %s session%s with heart-rate data so far, "
                                  "so this uses simple rules" % (n, act_word, "" if n == 1 else "s"))
    if body_from:
        reasons.insert(0, "Last night's Oura data isn't synced yet, so this uses your most recent "
                          "body data (%s). Opening the Oura app may give a fresher check" % body_from)
    elif not has_today:
        reasons.append("Today's Oura data isn't in yet, so this leans on your history")
    lat = store.latest_hr
    if lat is not None and (now - lat).total_seconds() / 60.0 > STALE_MINUTES:
        reasons.append("Oura data is %s old \u2014 syncing your ring may give a fresher check"
                       % fmt_hm((now - lat).total_seconds() / 60.0))
    seen, uniq = set(), []
    for t in reasons:
        if t not in seen:
            seen.add(t); uniq.append(t)
    reasons = uniq[:4]
    if len(reasons) < 2:
        reasons.append("Changing plans is always okay \u2014 you know your body best")

    # ---- options (by kind and traits)
    options = build_options(verdict, activity, config, start, duration_min, now, tz, thr,
                            pool_for_shorten, typical_rate, usable)

    # ---- late-cancel deadline (user-set; never assumed)
    cancel_by = None
    lch = adef.get("late_cancel_hours")
    if isinstance(lch, (int, float)) and not isinstance(lch, bool):
        cancel_by = start - timedelta(hours=lch)

    headline = HEADLINES[verdict]
    kind = "preview" if start.astimezone(tz).date() > now.astimezone(tz).date() else "precheck"
    if cancel_by is not None and verdict != "green":
        if now >= cancel_by:
            if kind == "preview":
                cancel_note = ("The late-cancel window has already passed — that's okay; a gentler "
                               "version is still a good option")
            else:
                cancel_note = None
        else:
            cancel_note = "If you'd rather cancel, deciding by %s avoids a late-cancel fee" % (
                fmt_clock(cancel_by.astimezone(tz)) if cancel_by.astimezone(tz).date() == now.astimezone(tz).date()
                else cancel_by.astimezone(tz).strftime("%a ") + fmt_clock(cancel_by.astimezone(tz)))
        if cancel_note:
            reasons = reasons[:3] + [cancel_note]
    if kind == "preview":
        # Future day: tonight's sleep isn't known yet, so this is an early look only.
        headline = {"green": "Early look: looks like a good fit",
                    "yellow": "Early look: maybe plan it a little easier",
                    "red": "Early look: a gentler version may suit"}[verdict]
        lc = [r for r in reasons if "late-cancel" in r][:1]
        reasons = [r for r in reasons if "late-cancel" not in r][:3 - len(lc)] + lc + \
            ["Based on today's body data. I'll re-check with fresh data before it starts"]
        if confidence == "good":
            confidence = "moderate"

    precheck = int(config.get("precheck_minutes", 60))
    check_due = start - timedelta(minutes=precheck)
    ev = event or {}
    return {
        "event_id": ev.get("id") or "manual-%s-%s" % (activity, start.astimezone(timezone.utc).strftime("%Y%m%dT%H%M")),
        "title": ev.get("title") or ("%s (check now)" % label_for(activity, config)),
        "activity_kind": akind,
        "label": label_for(activity, config),
        "start": iso(start.astimezone(tz)), "end": iso(end.astimezone(tz)),
        "duration_min": duration_min, "activity": activity,
        "check_due": iso(check_due.astimezone(tz)),
        "status": "checked" if now >= check_due else "upcoming",
        "kind": kind,
        "cancel_by": iso(cancel_by.astimezone(tz)) if cancel_by is not None else None,
        "verdict": verdict, "confidence": confidence, "headline": headline,
        "reasons": reasons,
        "expected": {
            "minutes_above_threshold": _r(expected_rate * scale) if expected_rate is not None else None,
            "typical_minutes_above_threshold": _r(typical_rate * scale) if typical_rate is not None else None,
            "peak_hr": _r(expected_peak),
        },
        "similar_days": [{
            "workout_id": r["w"]["id"], "date": r["w"]["day"], "duration_min": r["m"]["duration_min"],
            "avg_hr": _r(r["m"]["avg_hr"]), "peak_hr": _r(r["m"]["peak_hr"]),
            "minutes_above_threshold": r["m"]["minutes_above_threshold"],
            "readiness": r["f"].get("readiness"), "hrv_delta_pct": _r(r["f"].get("hrv_delta_pct"), 1),
            "similarity": round(sim, 2), "activity": r["w"]["activity"],
        } for sim, _d, r in neighbours],
        "options": options,
    }


# =============================================================== public API
def check(db_path, config, activity, start, duration_min, now=None, event=None):
    """Run one pre-activity check -> CHECK dict (SPEC shape)."""
    store = get_store(db_path)
    now = now_in(config, now)
    return _check(store, config, activity, start, duration_min, now, event)


def build_today(db_path, config, events, now=None, mode=None, calendar_connected=None,
                calendar_message=None):
    """GET /api/today payload. `events` = normalised calendar events (any range; past ones dropped)."""
    store = get_store(db_path)
    now = now_in(config, now)
    tz = get_tz(config)
    today = now.date().isoformat()
    f = store.day_features(today, partial=True)
    status, headline, _pts = day_assessment(f, config.get("caution_style", "balanced"))
    _sleep_base = store._baseline(today, lambda d: (store.long_sleep.get(d) or {}).get("total_sleep_duration"))
    upcoming = []
    for ev in events or []:
        act = ev.get("activity") or classify(ev, config)
        if not act:
            continue
        s, e = parse_iso(ev.get("start"), tz), parse_iso(ev.get("end"), tz)
        if not s or not e or e <= now:
            continue
        dur = max(5, int(round((e - s).total_seconds() / 60)))
        upcoming.append(_check(store, config, act, s, dur, now, ev))
    upcoming.sort(key=lambda c: c["start"])
    out = {
        "generated_at": iso(now),
        "mode": mode or ("mock" if "mock" in Path(db_path).name else "real"),
        "freshness": _freshness(store, now),
        "body": {
            "readiness": f.get("readiness"), "readiness_baseline": _r(f.get("readiness_baseline")),
            "sleep_score": f.get("sleep_score"),
            "total_sleep_min": _r(f.get("total_sleep_min")),
            "total_sleep_baseline_min": _r(_sleep_base / 60.0) if _sleep_base else None,
            "hrv": _r(f.get("hrv")), "hrv_baseline": _r(f.get("hrv_baseline")),
            "hrv_delta_pct": _r(f.get("hrv_delta_pct"), 1),
            "rhr": _r(f.get("rhr")), "rhr_baseline": _r(f.get("rhr_baseline")),
            "rhr_delta": _r(f.get("rhr_delta")),
            "stress_summary": f.get("stress_summary"), "steps_so_far": f.get("steps"),
            "temperature_deviation": f.get("temperature_deviation"),
        },
        "day_status": status,
        "day_headline": headline,
        "upcoming": upcoming,
        "settings": config,
    }
    if calendar_connected is not None:
        out["calendar_connected"] = bool(calendar_connected)
    if calendar_message:
        out["calendar_message"] = calendar_message
    return out


VERDICT_RANK = {"green": 0, "yellow": 1, "red": 2}
TOMORROW_HEADLINES = {
    "green": "Tomorrow: looks like a good fit",
    "yellow": "Tomorrow: an easier plan may suit",
    "red": "Tomorrow: a gentler plan may suit",
}


def _preview_time(config, day_dt):
    """`day_dt` (local midnight) at config preview.time (default 20:00)."""
    from planwell.settings import parse_hhmm
    hm = parse_hhmm(((config or {}).get("preview") or {}).get("time", "20:00")) or (20, 0)
    return day_dt.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)


def preview_due_for(start, activity, config, now):
    """min(today at preview.time, start - late_cancel_hours - 60 min); 2nd term only if hours are set."""
    tz = get_tz(config)
    now = now.astimezone(tz)
    due = _preview_time(config, now.replace(hour=0, minute=0, second=0, microsecond=0))
    lch = ((config or {}).get("late_cancel_hours") or {}).get(norm_activity(activity))
    if isinstance(lch, (int, float)) and not isinstance(lch, bool):
        due = min(due, start.astimezone(tz) - timedelta(hours=lch, minutes=60))
    return due


def _recheck_phrase(config):
    m = int((config or {}).get("precheck_minutes", 60))
    if m % 60 == 0:
        h = m // 60
        return "%d hour%s" % (h, "" if h == 1 else "s")
    return "%d minutes" % m


def build_tomorrow(db_path, config, events, now=None):
    """GET /api/tomorrow payload: early-look checks (kind "preview") for tomorrow's walks/Solidcore."""
    store = get_store(db_path)
    now = now_in(config, now)
    tz = get_tz(config)
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow = (now.date() + timedelta(days=1))
    checks, dues = [], []
    for ev in events or []:
        act = ev.get("activity") or classify(ev, config)
        if not act or kind_of(act, config) != "exercise":
            continue
        s, e = parse_iso(ev.get("start"), tz), parse_iso(ev.get("end"), tz)
        if not s or not e or s.astimezone(tz).date() != tomorrow:
            continue
        dur = max(5, int(round((e - s).total_seconds() / 60)))
        c = _check(store, config, act, s, dur, now, ev)
        c["kind"] = "preview"
        checks.append(c)
        dues.append(preview_due_for(s, act, config, now))
    checks.sort(key=lambda c: c["start"])
    if checks:
        status = max((c["verdict"] for c in checks), key=lambda v: VERDICT_RANK[v])
        headline = TOMORROW_HEADLINES[status]
        due = min(dues)
    else:
        f = store.day_features(now.date().isoformat(), partial=True)
        status, _h, _p = day_assessment(f, config.get("caution_style", "balanced"))
        headline = "Nothing tracked on your calendar for tomorrow."
        due = _preview_time(config, day0)
    return {
        "date": tomorrow.isoformat(),
        "generated_at": iso(now),
        "day_status": status,
        "kind": "preview",
        "headline": headline,
        "note": "Based on today's body data. I'll re-check %s before each session." % _recheck_phrase(config),
        "preview_due": iso(due),
        "events": checks,
    }


LOAD_HEADLINES = {
    "light": "A lighter day than usual",
    "usual": "A fairly usual day",
    "heavy": "A heavier day than usual",
    "very_heavy": "A much heavier day than usual",
}
LOAD_WORDS = {"light": "lighter than usual", "usual": "usual", "heavy": "heavier than usual",
              "very_heavy": "much heavier than usual"}
HEAVY = ("heavy", "very_heavy")
LEVELS = ["light", "usual", "heavy", "very_heavy"]


DAILY_LOAD_WEIGHT = 0.5  # daily-kind events (classes) count for less than exercise in the level


def _is_exercise_rec(r):
    return r["w"].get("kind", "exercise") == "exercise"


def typical_daily_load(recs, today, days=60):
    """Like-with-like typical: median exercise minutes above threshold over the last `days`,
    counting only days that had at least one exercise session (rest/class-only days excluded)."""
    lo = add_days(today, -days)
    loads = _load_by_day([r for r in recs if lo <= r["w"]["day"] < today and _is_exercise_rec(r)])
    return _median(list(loads.values())) if loads else None


LOAD_SMOOTH = 5.0
TOMORROW_SHRINK = 0.75  # tomorrow is an early look (tonight's sleep unknown): pull toward typical
LOAD_FLOOR = 5.0  # min "typical" used for the ratio, so near-zero typical days don't make everything heavy


def load_level(expected, typical, has_exercise=True):
    """light|usual|heavy|very_heavy for an exercise-weighted load vs the typical exercise day.
    A day without exercise is compared on its own terms: light unless its daily load is substantial."""
    if not has_exercise:
        return "usual" if float(expected or 0) >= 0.7 * max(float(typical or 0), LOAD_FLOOR) else "light"
    # +LOAD_SMOOTH on both sides so a few minutes on a calm plan don't swing the ratio
    ratio = (float(expected or 0) + LOAD_SMOOTH) / (max(float(typical or 0), LOAD_FLOOR) + LOAD_SMOOTH)
    if ratio < 0.7:
        return "light"
    if ratio <= 1.2:
        return "usual"
    if ratio <= 1.6:
        return "heavy"
    return "very_heavy"


def _safe_label(ev, config):
    try:
        from planwell.classify import label as _label
        return _label(ev, config)
    except Exception:
        return None


def _pair_history(recs, prev_act, act):
    """(mean rate of `act` sessions the day after a `prev_act` session, mean otherwise, n_after)."""
    days_prev = {r["w"]["day"] for r in recs if r["w"]["activity"] == prev_act}
    after, other = [], []
    for r in recs:
        if r["w"]["activity"] != act or r["rate"] is None:
            continue
        (after if add_days(r["w"]["day"], -1) in days_prev else other).append(r["rate"])
    return _mean(after), _mean(other), len(after)


def _day_suggestions(day_items, prev_acts, prev_level, level, recs, config, thr, is_today, tz):
    out = []
    tracked = [it for it in day_items if it["tracked"] and it["check"]]
    exercise = [it for it in tracked if kind_of(it["activity"], config) == "exercise"]
    # 1) history: "<A> yesterday + <B> today has been hard before"
    for it in exercise:
        for pa in sorted(prev_acts):
            m_after, m_other, n_after = _pair_history(recs, pa, it["activity"])
            if n_after >= 3 and m_after is not None and m_other is not None and \
                    m_after >= 1.3 * m_other + 1.0:
                out.append({"type": "space", "event_id": it["event_id"],
                            "text": "%s %s + %s %s has been harder before (about %d vs %d min above %d bpm "
                                    "per 30 min). Consider %s on another day."
                                    % (label_for(pa, config), "yesterday" if is_today else "today",
                                       word_for(it["activity"], config), "today" if is_today else "tomorrow",
                                       round(m_after), round(m_other), thr, word_for(it["activity"], config))})
                break
    # 2) back-to-back heavy days
    if prev_level in HEAVY and level in HEAVY and exercise:
        heavy = max(exercise, key=lambda it: (it["check"]["expected"].get("minutes_above_threshold") or 0))
        if not any(sg["event_id"] == heavy["event_id"] for sg in out):
            out.append({"type": "space", "event_id": heavy["event_id"],
                        "text": ("Yesterday was a heavier day and today looks heavy too \u2014 spacing things out "
                                 "may help, for example moving %s to another day." if is_today else
                                 "Today and tomorrow both look heavy \u2014 moving %s to another day may help.")
                        % word_for(heavy["activity"], config)})
    # 3) rest block between close tracked items
    for a, b in zip(tracked, tracked[1:]):
        gap = (parse_iso(b["start"], tz) - parse_iso(a["end"], tz)).total_seconds() / 60.0
        if gap < 45 and "exercise" in (kind_of(a["activity"], config), kind_of(b["activity"], config)):
            out.append({"type": "rest_block", "event_id": b["event_id"],
                        "text": "Plan a short rest between %s and %s." % (a["label"], word_for(b["activity"], config))})
            break
    # 4) a red session with a move/shorten option
    for it in exercise:
        if it["check"]["verdict"] != "red":
            continue
        for o in it["check"]["options"]:
            if o["type"] in ("move", "shorten"):
                out.append({"type": o["type"], "event_id": it["event_id"],
                            "text": "%s at %s: %s may suit better." % (
                                it["label"], fmt_clock(parse_iso(it["start"], tz).astimezone(tz)),
                                o["label"][0].lower() + o["label"][1:])})
                break
        else:
            continue
        break
    return out[:3]


def build_next48(db_path, config, events, now=None):
    """GET /api/next48 payload: today + tomorrow, load vs typical, items with checks, suggestions."""
    store = get_store(db_path)
    now = now_in(config, now)
    tz = get_tz(config)
    thr = config.get("hr_threshold_bpm", 125)
    today = now.date().isoformat()
    recs = _session_records(store, config, None, before=now)
    ex_loads = _load_by_day([r for r in recs if _is_exercise_rec(r)])
    dl_loads = _load_by_day([r for r in recs if not _is_exercise_rec(r)])
    typical = typical_daily_load(recs, today)
    yday = add_days(today, -1)
    lo60 = add_days(today, -60)
    counts = {}
    for r in recs:
        if _is_exercise_rec(r) and lo60 <= r["w"]["day"] < today:
            counts[r["w"]["day"]] = counts.get(r["w"]["day"], 0) + 1
    typical_count = _median(list(counts.values()))
    n_ex_done_today = sum(1 for r in recs if _is_exercise_rec(r) and r["w"]["day"] == today)
    # yesterday, like-with-like: actual minutes vs what those same sessions usually cost
    med_rate = {}
    for r in recs:
        if r["rate"] is not None:
            med_rate.setdefault(r["w"]["activity"], []).append(r["rate"])
    med_rate = {a: _median(v) for a, v in med_rate.items()}
    y_exp = y_typ = 0.0
    for r in recs:
        if r["w"]["day"] != yday or r["rate"] is None:
            continue
        wgt = 1.0 if _is_exercise_rec(r) else DAILY_LOAD_WEIGHT
        y_exp += wgt * (r["m"]["minutes_above_threshold"] or 0)
        y_typ += wgt * med_rate.get(r["w"]["activity"], 0) * r["m"]["duration_min"] / 30.0
    prev_level = load_level(y_exp, y_typ, has_exercise=yday in ex_loads)
    prev_acts = {r["w"]["activity"] for r in recs if r["w"]["day"] == add_days(today, -1)}
    evs = []
    for ev in events or []:
        s_, e_ = parse_iso(ev.get("start"), tz), parse_iso(ev.get("end"), tz)
        if s_ and e_ and e_ > s_ and not ev.get("all_day") and not ev.get("declined"):
            evs.append((s_, e_, ev))
    evs.sort(key=lambda x: x[0])
    days = []
    for offset, name in ((0, "Today"), (1, "Tomorrow")):
        d = add_days(today, offset)
        items = []
        for s_, e_, ev in evs:
            if s_.astimezone(tz).date().isoformat() != d or e_ <= now:
                continue
            act = ev.get("activity") or classify(ev, config)
            chk = None
            if act:
                dur = max(5, int(round((e_ - s_).total_seconds() / 60)))
                chk = _check(store, config, act, s_, dur, now, ev)
            ai = _safe_label(ev, config)
            suggest = None
            if not act and ai and ai.get("category") in registry(config) and (ai.get("exertion") or 0) >= 3:
                suggest = ai["category"]
            items.append({"event_id": ev.get("id"), "title": ev.get("title") or "",
                          "start": iso(s_.astimezone(tz)), "end": iso(e_.astimezone(tz)),
                          "activity": act, "label": label_for(act, config) if act else None,
                          "check": chk, "ai_label": ai, "tracked": bool(act),
                          "suggest_activity": suggest})
        # Like-with-like: compare the day's expected load with what THESE sessions usually cost
        # (sum of each check's typical), so a Solidcore day isn't "heavy" just because walks are calm.
        exp_ex = exp_dl = typ_ex = typ_dl = 0.0
        n_ex = 0
        for it in items:
            if not it["check"]:
                continue
            v = it["check"]["expected"].get("minutes_above_threshold") or 0
            tv = it["check"]["expected"].get("typical_minutes_above_threshold")
            tv = v if tv is None else tv
            if offset == 1:
                v = tv + TOMORROW_SHRINK * (v - tv)
            if kind_of(it["activity"], config) == "exercise":
                exp_ex += v
                typ_ex += tv
                n_ex += 1
            else:
                exp_dl += v
                typ_dl += tv
        if offset == 0:  # sessions already done today count on both sides
            exp_ex += ex_loads.get(today, 0)
            typ_ex += ex_loads.get(today, 0)
            exp_dl += dl_loads.get(today, 0)
            typ_dl += dl_loads.get(today, 0)
            n_ex += n_ex_done_today
        expected = exp_ex + exp_dl
        typical_plan = typ_ex + typ_dl
        level = load_level(exp_ex + DAILY_LOAD_WEIGHT * exp_dl, typ_ex + DAILY_LOAD_WEIGHT * typ_dl,
                           has_exercise=n_ex > 0)
        if n_ex >= (typical_count or 1) + 2:  # unusually many exercise sessions in one day
            level = LEVELS[min(len(LEVELS) - 1, LEVELS.index(level) + 1)]
        has_tracked = any(it["tracked"] for it in items)
        headline = LOAD_HEADLINES[level] if (has_tracked or expected) else "No tracked activities planned"
        sugg = _day_suggestions(items, prev_acts, prev_level, level, recs, config, thr, offset == 0, tz)
        days.append({"date": d, "label": name,
                     "load": {"expected": int(round(expected)), "typical": int(round(typical_plan)),
                              "level": level, "typical_day": _r(typical)},
                     "headline": headline, "items": items, "suggestions": sugg})
        prev_level = level
        prev_acts = {it["activity"] for it in items if it["tracked"]
                     and kind_of(it["activity"], config) == "exercise"}
    return {"generated_at": iso(now), "days": days,
            "note": "Tomorrow's checks are early looks; each gets a fresh check %s before."
                    % _recheck_phrase(config)}


def sessions(db_path, config, activity=None, limit=60):
    """GET /api/sessions payload, newest first."""
    store = get_store(db_path)
    tz = get_tz(config)
    recs = _session_records(store, config, activity)
    recs.sort(key=lambda r: r["w"]["start"], reverse=True)
    try:
        limit = max(1, int(limit))
    except (TypeError, ValueError):
        limit = 60
    return {"sessions": [session_public(r, tz) for r in recs[:limit]],
            "zones_bpm": list(config.get("zones_bpm", [100, 115, 125, 140])),
            "hr_threshold_bpm": config.get("hr_threshold_bpm", 125)}


def session_detail(db_path, config, workout_id):
    """GET /api/session/<id> payload, or None if unknown."""
    store = get_store(db_path)
    tz = get_tz(config)
    rec = next((r for r in _session_records(store, config) if r["w"]["id"] == workout_id), None)
    if rec is None:
        return None
    w = rec["w"]
    hr = store.hr_between(w["start"] - timedelta(minutes=5), w["end"] + timedelta(minutes=POST_WINDOW_MIN))
    return {"session": session_public(rec, tz),
            "hr": [{"t": iso(t.astimezone(tz)), "bpm": b} for t, b in hr]}
