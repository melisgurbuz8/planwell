"""PlanWell analytics: personal patterns (insights) and an honest, leak-free backtest.

Public API (shapes match SPEC "Addendum v2"):
    insights(db_path, config, now=None) -> dict      # GET /api/insights
    backtest(db_path, config, min_history=8, now=None) -> dict   # GET /api/backtest

Both reuse the engine's store, session metrics and day features, so the numbers match what the
rest of the app shows. Wording is plain and non-medical: these are patterns in the person's own
history, never predictions or advice.

Leak-free backtest: every past session is re-judged with engine._check() as of 60 min before it
started. The engine only uses sessions that had *ended* by `now`, and baselines only look at
earlier days. The one thing the engine can't know for a past day is how much of that day's steps
and stress had happened yet (Oura only stores daily totals), so for the judged day we scale those
totals by the time of day, exactly as the engine already does for every historical session.
"""
from datetime import timedelta

from planwell import engine, labeler
from planwell.timeutil import get_tz, iso, now_in

CAVEAT = "Patterns in your own history, not medical advice."
BACKTEST_METHOD = "Each session judged only with data from before it."
LEAD_MINUTES = 60
HARD_FLOOR = 3.0  # min above threshold per 30 min
BASELINE_READINESS = 70


# ------------------------------------------------------------------ helpers
def _registry(config):
    """Activity registry: settings.registry when available, else labeler.registry (pre-v3 configs)."""
    try:
        from planwell import settings as _settings
    except Exception:  # pragma: no cover
        _settings = None
    for mod, name in ((_settings, "registry"), (engine, "registry")):
        fn = getattr(mod, name, None)
        if callable(fn):
            try:
                reg = fn(config)
                if isinstance(reg, dict) and reg:
                    return reg
            except Exception:
                pass
    return labeler.registry(config)


def _label(activity, config, plural=False):
    """User-facing name: 'Solidcore', 'walk(s)', 'Padel', 'class(es)'."""
    spec = _registry(config).get(activity) or {}
    lab = spec.get("label") or ("Solidcore" if activity == "pilates" else activity.capitalize())
    if activity == "walking" or lab.lower() == "walk":
        return "walks" if plural else "walk"
    if spec.get("kind") == "daily" or lab.lower() == "class":
        low = lab.lower()
        if plural:
            return low + ("es" if low.endswith(("s", "x", "ch", "sh")) else "s")
        return low
    return lab


def _subject(activity, config):
    """Sentence subject: 'Solidcore', 'walks', 'sessions'."""
    if activity == "all":
        return "sessions"
    return _label(activity, config, plural=True)


def _cap(s):
    return s[:1].upper() + s[1:] if s else s


def _percentile(xs, q):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    if len(xs) == 1:
        return xs[0]
    pos = (len(xs) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _confidence(n_a, n_b):
    n = min(n_a, n_b)
    if n < 6:
        return "low"
    if n < 15:
        return "medium"
    return "high"


def _r1(x):
    return None if x is None else round(float(x), 1)


def _records(store, config, now):
    """Usable session records (rate known) that ended by `now`, oldest first."""
    recs = engine._session_records(store, config, None, before=now)
    return [r for r in recs if r["rate"] is not None]


def _wanted(config):
    """All activity keys in the registry (exercise first, then daily kinds)."""
    reg = _registry(config)
    keys = list(reg) or ["walking", "pilates"]
    return sorted(keys, key=lambda k: (reg.get(k, {}).get("kind") == "daily", keys.index(k)))


def _exercise(config):
    reg = _registry(config)
    return [k for k in _wanted(config) if (reg.get(k) or {}).get("kind", "exercise") != "daily"]


def _hard_cutoffs(recs, activities):
    out = {}
    for act in activities:
        rates = [r["rate"] for r in recs if r["w"]["activity"] == act]
        p75 = _percentile(rates, 0.75)
        out[act] = max(HARD_FLOOR, p75) if p75 is not None else HARD_FLOOR
    return out


# ------------------------------------------------------------------ insights
def _ratio_phrase(a, b):
    """How group a compares with group b, in words ('about 3× harder', 'a bit calmer')."""
    ratio = (a + 0.5) / (b + 0.5)  # +0.5 softens tiny denominators (calm walks are often ~0)
    if ratio >= 1.35:
        if b < 0.5 or ratio >= 4.25:
            return "more than 4× harder" if b >= 0.5 else "noticeably harder", ratio
        if ratio < 1.75:
            return "about 1.5× harder", ratio
        n = round(ratio * 2) / 2.0
        txt = ("%d" % n) if n == int(n) else ("%.1f" % n)
        return "about %s× harder" % txt, ratio
    if ratio >= 1.12:
        return "a bit harder", ratio
    if ratio > 1 / 1.12:
        return "about the same", ratio
    if ratio > 1 / 1.35:
        return "a bit calmer", ratio
    return "noticeably calmer", ratio


def _group_value(rs):
    return _mean([r["rate"] for r in rs])


MIN_GROUP = 4
MIN_CONTRAST = 0.25  # |ratio - 1| worth talking about


def _best_split(recs, config, activities, fn_a, fn_b, prefer=None):
    """Pick which activity (or all sessions) an insight talks about.

    `prefer` lets different insights talk about different activities (variety on the page):
    the preferred activity is used when both its groups have >= 4 sessions and the contrast is
    noticeable; otherwise the clearest valid contrast wins. Returns None when nothing is valid.
    """
    cands = []
    ex = set(_exercise(config))
    for act in list(activities) + ["all"]:
        pool = ([r for r in recs if r["w"]["activity"] in ex] if act == "all"
                else [r for r in recs if r["w"]["activity"] == act])
        ga = [r for r in pool if fn_a(r)]
        gb = [r for r in pool if fn_b(r)]
        if len(ga) < MIN_GROUP or len(gb) < MIN_GROUP:
            continue
        va, vb = _group_value(ga), _group_value(gb)
        contrast = abs((va + 0.5) / (vb + 0.5) - 1.0)
        score = min(contrast, 2.0) * (0.8 if act == "all" else 1.0)
        cands.append((score, act, ga, gb, va, vb))
    if not cands:
        return None
    for c in cands:
        if c[1] == prefer and c[0] >= MIN_CONTRAST:
            return c
    return max(cands, key=lambda c: c[0])


def _f(r, key):
    return r["f"].get(key)


def _insight_contrast(iid, title, recs, config, activities, fn_a, fn_b, label_a, label_b, sentence,
                      prefer=None):
    best = _best_split(recs, config, activities, fn_a, fn_b, prefer)
    if best is None:
        return None
    _c, act, ga, gb, va, vb = best
    phrase, _ratio = _ratio_phrase(va, vb)
    return {
        "id": iid, "title": title,
        "finding": sentence(_subject(act, config), phrase),
        "activity": act,
        "a": {"label": label_a, "value": _r1(va), "n": len(ga)},
        "b": {"label": label_b, "value": _r1(vb), "n": len(gb)},
        "confidence": _confidence(len(ga), len(gb)),
    }


def _sentence(prefix, subj, phrase, compare):
    """'After nights under 6 h, Solidcore ran about 3× harder.' style findings."""
    if phrase == "about the same":
        return "%s, %s ran about the same as %s." % (prefix, subj, compare)
    if "×" in phrase:
        return "%s, %s ran %s." % (prefix, subj, phrase)
    return "%s, %s ran %s than %s." % (prefix, subj, phrase, compare)


def _sleep_insight(recs, config, acts):
    return _insight_contrast(
        "sleep", "Short sleep", recs, config, acts,
        lambda r: _f(r, "total_sleep_min") is not None and _f(r, "total_sleep_min") < 360,
        lambda r: _f(r, "total_sleep_min") is not None and _f(r, "total_sleep_min") >= 420,
        "< 6 h sleep", "7 h +",
        lambda subj, ph: _sentence("After nights under 6 h", subj, ph, "after 7 h or more"),
        prefer="pilates")


def _hrv_insight(recs, config, acts):
    return _insight_contrast(
        "hrv", "Low HRV", recs, config, acts,
        lambda r: _f(r, "hrv_delta_pct") is not None and _f(r, "hrv_delta_pct") <= -15,
        lambda r: _f(r, "hrv_delta_pct") is not None and _f(r, "hrv_delta_pct") >= -5,
        "HRV 15%+ below usual", "HRV near or above usual",
        lambda subj, ph: _sentence("On days your HRV was 15% or more below usual", subj, ph, "on usual days"),
        prefer="walking")


def _readiness_insight(recs, config, acts):
    return _insight_contrast(
        "readiness", "Lower readiness", recs, config, acts,
        lambda r: _f(r, "readiness") is not None and _f(r, "readiness") < 70,
        lambda r: _f(r, "readiness") is not None and _f(r, "readiness") >= 80,
        "Readiness < 70", "Readiness 80 +",
        lambda subj, ph: _sentence("With readiness under 70", subj, ph, "on 80+ days"),
        prefer="pilates")


def _time_insight(recs, config, acts):
    """Mornings (before 11) vs late afternoon / evening (from 16:00)."""
    def is_am(r):
        h = _f(r, "hour_of_day")
        return h is not None and h < 11

    def is_pm(r):
        h = _f(r, "hour_of_day")
        return h is not None and h >= 16

    best = _best_split(recs, config, acts, is_am, is_pm, prefer="walking")
    if best is None:
        return None
    _c, act, ga, gb, va, vb = best
    subj = _subject(act, config)
    # phrase from the harder group's point of view so it always reads naturally
    if va >= vb:
        phrase, _ = _ratio_phrase(va, vb)
        harder, easier = "morning", "late-day"
    else:
        phrase, _ = _ratio_phrase(vb, va)
        harder, easier = "late-day", "morning"
    if phrase == "about the same":
        finding = "Morning and late-day %s have run about the same." % subj
    else:
        finding = "Your %s %s ran %s than %s ones." % (harder, subj, phrase, easier)
    return {"id": "time_of_day", "title": "Time of day", "finding": finding, "activity": act,
            "a": {"label": "Before 11 am", "value": _r1(va), "n": len(ga)},
            "b": {"label": "4 pm or later", "value": _r1(vb), "n": len(gb)},
            "confidence": _confidence(len(ga), len(gb))}


def _activity_compare(recs, config):
    """Solidcore vs walks (or the two most-logged exercise activities)."""
    ex = _exercise(config)
    counts = {a: sum(1 for r in recs if r["w"]["activity"] == a) for a in ex}
    pair = ("pilates", "walking") if counts.get("pilates", 0) >= 4 and counts.get("walking", 0) >= 4 else \
        tuple(sorted(counts, key=lambda a: -counts[a])[:2])
    if len(pair) < 2:
        return None
    ga = [r for r in recs if r["w"]["activity"] == pair[0]]
    gb = [r for r in recs if r["w"]["activity"] == pair[1]]
    if len(ga) < 4 or len(gb) < 4:
        return None
    va, vb = _group_value(ga), _group_value(gb)
    na, nb = _label(pair[0], config, plural=True), _label(pair[1], config, plural=True)
    if va >= vb:
        phrase, _ = _ratio_phrase(va, vb)
        first, second = na, nb
    else:
        phrase, _ = _ratio_phrase(vb, va)
        first, second = nb, na
    if phrase == "about the same":
        finding = "%s and %s have kept your heart rate up about equally." % (_cap(na), nb)
    else:
        finding = "%s ran %s than %s, per 30 minutes." % (_cap(first), phrase, second)
    return {"id": "activity_compare", "title": "%s vs %s" % (_cap(nb), _cap(na)), "finding": finding,
            "activity": "all",
            "a": {"label": _cap(_label(pair[0], config)), "value": _r1(va), "n": len(ga)},
            "b": {"label": _cap(nb), "value": _r1(vb), "n": len(gb)},
            "confidence": _confidence(len(ga), len(gb))}


def _recovery_trend(recs, config, now):
    """Heart-rate drop ~10 min after sessions: last 8 weeks vs the 8 weeks before."""
    cut = now - timedelta(days=56)
    early_cut = now - timedelta(days=112)
    ex = set(_exercise(config))
    rec = [r for r in recs if r["m"].get("recovery_drop_bpm") is not None and r["w"]["activity"] in ex]
    ga = [r for r in rec if r["w"]["start"] >= cut]
    gb = [r for r in rec if early_cut <= r["w"]["start"] < cut]
    if len(ga) < 4 or len(gb) < 4:
        return None
    va = _mean([r["m"]["recovery_drop_bpm"] for r in ga])
    vb = _mean([r["m"]["recovery_drop_bpm"] for r in gb])
    diff = va - vb
    if diff >= 3:
        finding = "Your heart rate has been settling a little faster after sessions lately (%d vs %d bpm drop in ~10 min)." \
                  % (round(va), round(vb))
    elif diff <= -3:
        finding = ("Your heart rate has been settling a little more slowly after sessions lately "
                   "(%d vs %d bpm drop in ~10 min) — extra rest after may help." % (round(va), round(vb)))
    else:
        finding = "How quickly your heart rate settles after sessions has stayed about the same (around %d bpm drop in ~10 min)." \
                  % round(va)
    return {"id": "recovery_trend", "title": "Recovery after sessions", "finding": finding, "activity": "all",
            "a": {"label": "Last 8 weeks", "value": _r1(va), "n": len(ga)},
            "b": {"label": "8 weeks before", "value": _r1(vb), "n": len(gb)},
            "confidence": _confidence(len(ga), len(gb)),
            "unit": "bpm drop ~10 min after"}


def insights(db_path, config, now=None):
    """GET /api/insights payload."""
    store = engine.get_store(db_path)
    now = now_in(config, now)
    acts = _wanted(config)
    recs = _records(store, config, now)
    thr = config.get("hr_threshold_bpm", 125)
    items = [
        _sleep_insight(recs, config, acts),
        _hrv_insight(recs, config, acts),
        _readiness_insight(recs, config, acts),
        _time_insight(recs, config, acts),
        _activity_compare(recs, config),
        _recovery_trend(recs, config, now),
    ]
    return {
        "generated_at": iso(now),
        "n_sessions": {a: sum(1 for r in recs if r["w"]["activity"] == a) for a in acts},
        "metric": "minutes above %d bpm per 30 min" % thr,
        "insights": [i for i in items if i],
        "caveat": CAVEAT,
    }


# ------------------------------------------------------------------ backtest
class _AsOfStore(object):
    """Store view for one replayed check: the judged day's steps/stress are 'so far', not full-day."""

    def __init__(self, store, day, hour):
        self._store, self._day, self._hour = store, day, hour

    def day_features(self, day, hour=None, partial=False):
        if partial and day == self._day:
            return self._store.day_features(day, self._hour)
        return self._store.day_features(day, hour, partial)

    def __getattr__(self, name):
        return getattr(self._store, name)


def _confusion(pairs):
    """pairs = [(flagged, hard)] -> counts + precision/recall/flag_rate."""
    tp = sum(1 for f, h in pairs if f and h)
    fp = sum(1 for f, h in pairs if f and not h)
    fn = sum(1 for f, h in pairs if not f and h)
    tn = sum(1 for f, h in pairs if not f and not h)
    n = len(pairs)
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "precision": round(tp / float(tp + fp), 2) if tp + fp else 0,
            "recall": round(tp / float(tp + fn), 2) if tp + fn else 0,
            "flag_rate": round((tp + fp) / float(n), 2) if n else 0}


def _fmt_cut(x):
    return ("%d" % x) if abs(x - round(x)) < 0.05 else ("%.1f" % x)


def replay_check(store, config, rec, lead_minutes=LEAD_MINUTES):
    """The CHECK the engine would have produced `lead_minutes` before this past session."""
    tz = get_tz(config)
    w, m = rec["w"], rec["m"]
    now = (w["start"] - timedelta(minutes=lead_minutes)).astimezone(tz)
    view = _AsOfStore(store, now.date().isoformat(), now.hour + now.minute / 60.0)
    event = {"id": w["id"], "title": _cap(_label(w["activity"], config))}
    return engine._check(view, config, w["activity"], w["start"], m["duration_min"], now, event)


def backtest(db_path, config, min_history=8, now=None):
    """GET /api/backtest payload: replay every past session in time order, leak-free."""
    store = engine.get_store(db_path)
    now = now_in(config, now)
    thr = config.get("hr_threshold_bpm", 125)
    acts = _wanted(config)
    recs = _records(store, config, now)
    recs.sort(key=lambda r: r["w"]["start"])
    cut = _hard_cutoffs(recs, acts)

    rows = []
    skipped = 0
    for r in recs:
        act = r["w"]["activity"]
        # earlier usable sessions of this activity that had ended before the check time
        check_at = r["w"]["start"] - timedelta(minutes=LEAD_MINUTES)
        prior = sum(1 for x in recs if x["w"]["activity"] == act and x["w"]["end"] <= check_at)
        if prior < min_history:
            skipped += 1
            continue
        c = replay_check(store, config, r)
        rd = r["f"].get("readiness")
        rows.append({
            "workout_id": r["w"]["id"], "date": r["w"]["day"], "activity": act,
            "verdict": c["verdict"], "minutes_per_30": round(r["rate"], 1),
            "hard": r["rate"] >= cut.get(act, HARD_FLOOR),
            "_base": rd is not None and rd < BASELINE_READINESS,
        })

    pw = _confusion([(x["verdict"] != "green", x["hard"]) for x in rows])
    base = dict({"name": "Readiness under %d only" % BASELINE_READINESS},
                **_confusion([(x["_base"], x["hard"]) for x in rows]))
    by_verdict = []
    for v in ("green", "yellow", "red"):
        g = [x for x in rows if x["verdict"] == v]
        by_verdict.append({"verdict": v, "n": len(g),
                           "avg_minutes": round(_mean([x["minutes_per_30"] for x in g]), 1) if g else None,
                           "pct_hard": round(sum(1 for x in g if x["hard"]) / float(len(g)), 2) if g else None})
    by_activity = {}
    for act in acts:
        g = [x for x in rows if x["activity"] == act]
        by_activity[act] = dict({"n": len(g)}, **_confusion([(x["verdict"] != "green", x["hard"]) for x in g]))

    parts = []
    for act in acts:
        if act in cut:
            parts.append("%s ≥ %s" % (_cap(_label(act, config, plural=True)), _fmt_cut(cut[act])))
    if len(set(round(v, 1) for v in cut.values())) <= 1 and cut:
        hard_def = "≥ %s min above %d bpm per 30 min (your top 25%%)" % (_fmt_cut(list(cut.values())[0]), thr)
    else:
        hard_def = "%s min above %d bpm per 30 min (your top 25%% for each)" % ("; ".join(parts), thr)

    for x in rows:
        x.pop("_base", None)
    return {
        "generated_at": iso(now),
        "n_sessions": len(rows),
        "n_skipped": skipped,
        "method": BACKTEST_METHOD,
        "hard_definition": hard_def,
        "hard_cutoffs": {a: round(v, 1) for a, v in cut.items()},
        "planwell": pw,
        "baseline": base,
        "by_verdict": by_verdict,
        "by_activity": by_activity,
        "timeline": rows,
    }
