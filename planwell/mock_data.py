"""Synthetic, POTS-like data for building and demoing PlanWell without real Oura data.

    python3 -m planwell.mock_data [--days 180] [--seed 7] [--today good|meh|poor]
                                  [--demo-calendar] [--now ISO] [--db PATH] [--calendar PATH]

Writes data/mock.db (same schema as data/planwell.db; recreated from scratch) and
data/mock_calendar.json (events for today + 6 days). --demo-calendar places the demo events at
fixed clock times (today: class 9:30, Solidcore 17:00, walk 19:00; tomorrow: morning walk,
class 13:00, padel 19:00, plus untracked events incl. "Hot yoga w/ Deniz"); without it, today's
first walk starts ~75 min from now (watcher demo).

Model (all invented, not medical):
- Each day has a hidden state: HRV (AR(1) around 45 +/- 10), sleep length, stress,
  and "hot day" (summer-weighted). These drive readiness / sleep score.
- ~4-5 walks/week (20-60 min) and ~2 pilates (Solidcore)/week (45-55 min), local New York times.
- Padel (Oura activity "padel"): only 4 sessions in the last 5 weeks, evenings, bursty rallies
  -> the engine's cold-start "still learning" path.
- Classes ("BA780 class" Mon/Wed 9:30, "MSBA seminar" Thu 13:00, 90 min) for the whole period,
  stored in `calendar_events` (past events only), with sparse (2-3 min) seated daytime HR during
  class, occasional stand-up spikes (more on low-HRV days) and a short walk to campus beforehand.
- Heart rate is POTS-like: spikes on standing/transitions; pilates standing segments spike;
  sessions on low-HRV / short-sleep / hot / stressful days run noticeably higher, so the
  engine's similar-days logic sees a real mix of calm and hard sessions.
- Heartrate rows: every 5 min awake + asleep, every 30 s during workouts and the 30 min after.
  Stored in UTC ('+00:00') exactly like sync.py stores real data.
Units follow Oura: durations / activity times / stress minutes are SECONDS.
"""
import argparse
import json
import math
import random
from datetime import date, datetime, time, timedelta, timezone

from pathlib import Path

from planwell import db

# ---- America/New_York without tzdata (US DST rules since 2007) ------------
def _nth_sunday(year, month, n):
    d = date(year, month, 1)
    d += timedelta(days=(6 - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def ny_tz(local_dt):
    """tzinfo (-04:00 or -05:00) for a naive New York local datetime."""
    y = local_dt.year
    dst_start = datetime.combine(_nth_sunday(y, 3, 2), time(2))
    dst_end = datetime.combine(_nth_sunday(y, 11, 1), time(2))
    hours = -4 if dst_start <= local_dt < dst_end else -5
    return timezone(timedelta(hours=hours))


def local(d, hour, minute=0, second=0):
    """Aware NY datetime for a date + clock time (hour may exceed 23 / be negative)."""
    naive = datetime.combine(d, time(0)) + timedelta(hours=hour, minutes=minute, seconds=second)
    return naive.replace(tzinfo=ny_tz(naive))


def to_ny(aware_dt):
    """Convert an aware datetime to New York local time (aware)."""
    est = aware_dt.astimezone(timezone(timedelta(hours=-5))).replace(tzinfo=None)
    return aware_dt.astimezone(ny_tz(est))


def iso(dt):
    return dt.replace(microsecond=0).isoformat()


def utc(dt):
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


# ---- daily hidden state ----------------------------------------------------
TODAY_PRESETS = {  # hrv, sleep hours, stress, hot
    "good": (52.0, 7.8, 0.2, False),
    "meh": (39.5, 6.9, 0.6, False),
    "poor": (28.0, 4.9, 0.9, True),
}


def make_days(rng, start, today, preset):
    days, hrv = [], 45.0
    d = start
    while d <= today:
        hrv = 45 + 0.6 * (hrv - 45) + rng.gauss(0, 8)            # AR(1): ~45 +/- 10
        sleep_h = clamp(rng.gauss(7.0, 0.9) + 0.03 * (hrv - 45), 3.8, 9.5)
        stress = clamp(rng.random() ** 1.5 + (0.25 if d.weekday() < 5 else 0), 0, 1)
        summer = d.month in (6, 7, 8) or (d.month in (5, 9) and rng.random() < 0.4)
        hot = rng.random() < (0.45 if summer else 0.04)
        if d == today and preset in TODAY_PRESETS:
            hrv, sleep_h, stress, hot = TODAY_PRESETS[preset]
        hrv_z = (hrv - 45) / 10.0
        temp_dev = round(rng.gauss(0, 0.18) + (0.25 if hot else 0) + (0.2 if sleep_h < 5.5 else 0), 2)
        # How hard the body is working today, in "standard deviations of bad".
        strain = (-0.9 * hrv_z + 0.55 * max(0, 7 - sleep_h) + (0.9 if hot else 0)
                  + 0.85 * stress + rng.gauss(0, 0.45))
        lowest = int(round(clamp(56 - 2.2 * hrv_z + rng.gauss(0, 2.5) + (2 if hot else 0), 46, 70)))
        days.append({"day": d, "hrv": round(clamp(hrv, 15, 90), 1), "hrv_z": hrv_z,
                     "sleep_h": sleep_h, "stress": stress, "hot": hot, "temp_dev": temp_dev,
                     "strain": strain, "lowest": lowest})
        d += timedelta(days=1)
    return days


# ---- heart-rate generators ---------------------------------------------------
def walk_hr(rng, minutes, s, hour):
    """Per-30s HR for a walk. Starts with an orthostatic jump, drifts upward."""
    morning = 5 if hour < 10 else 0                      # POTS mornings are often harder
    strain = clamp(s["strain"], -1.5, 3.0)
    base = 105 + 6 * strain + morning
    out = []
    for i in range(int(minutes * 2)):
        t = i / 2.0
        drift = 0.16 * t * (1 + 0.3 * max(strain, 0))     # HR creeps up the longer you're upright
        start_jump = 14 * math.exp(-t / 2.0)              # standing up / getting going
        hill = 10 if rng.random() < 0.04 else 0
        out.append(base + drift + start_jump + hill + rng.gauss(0, 4))
    return out


def pilates_hr(rng, minutes, s):
    """Per-30s HR for pilates: mat segments calm, standing segments + transitions spike."""
    plan = [("stand", 5), ("mat", 14), ("stand", 8), ("mat", 10), ("stand", 7), ("mat", 6)]
    scale = minutes / float(sum(m for _, m in plan))
    out, prev = [], None
    for kind, m in plan:
        n = max(2, int(round(m * scale * 2)))
        for j in range(n):
            if kind == "mat":
                hr = 88 + 5 * s["strain"]
            else:
                hr = 115 + 13 * s["strain"]
            if prev == "mat" and kind == "stand" and j < 3:       # mat -> standing transition
                hr += 18 + 6 * max(s["strain"], 0)
            out.append(hr + rng.gauss(0, 4))
        prev = kind
    return out[: int(minutes * 2)] or out


def padel_hr(rng, minutes, s):
    """Per-30s HR for padel: bursty rallies (2-4 min) with short breaks between points/games."""
    out, t = [], 0
    strain = clamp(s["strain"], -1.5, 3.0)
    while len(out) < minutes * 2:
        rally = rng.randint(4, 8)                          # half-minutes
        for j in range(rally):
            out.append(122 + 9 * strain + 10 * math.sin(math.pi * j / rally) + rng.gauss(0, 5))
        for _j in range(rng.randint(2, 4)):                # break, side change, water
            out.append(104 + 6 * strain + rng.gauss(0, 4))
        t += 1
    return out[: int(minutes * 2)]


def class_hr(rng, minutes, s, rest_hr):
    """Sparse daytime HR (every 2-3 min) while seated in class; standing up / presenting spikes."""
    strain = clamp(s["strain"], -1.5, 3.0)
    out, t = [], rng.uniform(0, 2)
    while t < minutes:
        bpm = rest_hr + 6 + 3 * strain + 9 * math.exp(-t / 8.0)      # still settling after the walk in
        if rng.random() < 0.03 + 0.03 * max(strain, 0):               # stood up / presented
            bpm += rng.uniform(15, 28) + 6 * max(strain, 0)
        out.append((t, bpm + rng.gauss(0, 3)))
        t += rng.choice([2, 2.5, 3])
    return out


def recovery_hr(rng, last_hr, rest_hr, s):
    """30 min after a session, every 30 s: exponential decay, slower on hard days."""
    tau = 5.0 + 2.0 * max(s["strain"], 0)
    return [rest_hr + 8 + (last_hr - rest_hr - 8) * math.exp(-(i / 2.0) / tau) + rng.gauss(0, 3)
            for i in range(60)]


# ---- main generator -----------------------------------------------------------
def rid(rng):
    h = "%032x" % rng.getrandbits(128)
    return "%s-%s-%s-%s-%s" % (h[:8], h[8:12], h[12:16], h[16:20], h[20:])


def plan_workouts(rng, s, is_today):
    """List of (activity, local start datetime, minutes) for one day."""
    if is_today:
        return []                                   # today's sessions are still in the calendar
    d, out = s["day"], []
    pil_day = d.weekday() in (1, 4) and rng.random() < 0.92 or rng.random() < 0.04
    if pil_day:                                     # ~2 pilates / week (mostly Tue/Fri)
        hour = rng.choice([9, 10, 11, 17, 17.5])
        out.append(("pilates", local(d, hour), rng.choice([45, 50, 50, 55])))
    if rng.random() < 0.72:                         # ~4.5 walks / week
        hour = rng.choice([7.5, 8, 8.5, 9, 12, 13, 17, 17.5, 18, 18.5, 19])
        mins = rng.choice([20, 25, 30, 30, 35, 40, 45, 50, 60])
        cand = local(d, hour, rng.randint(0, 14))
        if not out or abs((out[0][1] - cand).total_seconds()) > 3 * 3600:
            out.append(("walking", cand, mins))
    return out


CLASS_SCHEDULE = {  # weekday -> (title, hour, minute, minutes, location)
    0: ("BA780 class", 9, 30, 90, "Hall B 204"),
    2: ("BA780 class", 9, 30, 90, "Hall B 204"),
    3: ("MSBA seminar", 13, 0, 90, "Hall C 110"),
}
PADEL_DAYS_BACK = (33, 24, 12, 5)  # only ~4 padel sessions in the last 5 weeks -> "still learning"


def class_for(d, xrng, force=False):
    """(title, start, minutes, location) for a class on date d, or None (a few are skipped)."""
    c = CLASS_SCHEDULE.get(d.weekday())
    if not c:
        return None
    skip = xrng.random() < 0.07
    if skip and not force:
        return None
    title, h, m, mins, loc = c
    return title, local(d, h, m), mins, loc


def generate(days=180, seed=7, today_preset="meh", db_path=None, cal_path=None, now=None,
             demo_calendar=False):
    rng = random.Random(seed)
    now = now or datetime.now(timezone.utc)
    now_local = to_ny(now)
    today = now_local.date()
    start = today - timedelta(days=days)

    path = Path(db_path) if db_path else db.MOCK_DB
    if path.exists():
        path.unlink()
    for ext in ("-wal", "-shm", "-journal"):
        p = path.with_name(path.name + ext)
        if p.exists():
            p.unlink()
    conn = db.connect(path)

    states = make_days(rng, start, today, today_preset)
    xrng = random.Random(seed * 7919 + 13)   # classes / padel: separate stream
    hr_rows = {}
    W, R, DS, SL, ST, DA, CE = [], [], [], [], [], [], []
    padel_days = {today - timedelta(days=b) for b in PADEL_DAYS_BACK}

    def add_hr(dt, bpm, source):
        if dt <= now:
            hr_rows[utc(dt)] = (int(round(clamp(bpm, 45, 195))), source)

    for s in states:
        d, is_today = s["day"], s["day"] == today
        rest_hr = s["lowest"] + 17 + 4 * max(s["strain"], 0)

        # Night before: long_sleep ending this morning (Oura's `day` = wake-up day).
        bed = local(d, -1 + rng.uniform(-0.8, 0.9))          # ~23:00 previous evening
        dur = int(s["sleep_h"] * 3600)
        eff = int(clamp(rng.gauss(88, 4) - 3 * s["stress"], 70, 98))
        in_bed = int(dur / (eff / 100.0))
        wake = bed + timedelta(seconds=in_bed)
        sleep_id = rid(rng)
        avg_hr = round(s["lowest"] + rng.uniform(3, 6), 2)
        sl = {"id": sleep_id, "day": d.isoformat(), "type": "long_sleep",
              "bedtime_start": iso(bed), "bedtime_end": iso(wake),
              "total_sleep_duration": dur, "average_hrv": s["hrv"], "average_heart_rate": avg_hr,
              "lowest_heart_rate": s["lowest"], "efficiency": eff, "time_in_bed": in_bed}
        SL.append(dict(sl, raw_json=dict(sl)))
        t = bed
        while t < wake:
            add_hr(t, s["lowest"] + rng.uniform(0, 8), "sleep")
            t += timedelta(minutes=5)
        if rng.random() < 0.08 and not is_today:             # occasional nap (not the main night)
            nb = local(d, 15 + rng.random())
            nap = {"id": rid(rng), "day": d.isoformat(), "type": "sleep", "bedtime_start": iso(nb),
                   "bedtime_end": iso(nb + timedelta(minutes=35)), "total_sleep_duration": 1800,
                   "average_hrv": round(s["hrv"] * 0.9, 1), "average_heart_rate": avg_hr + 6,
                   "lowest_heart_rate": s["lowest"] + 6, "efficiency": 80}
            SL.append(dict(nap, raw_json=dict(nap)))

        # Readiness / daily sleep.
        sleep_score = int(clamp(72 + 9 * (s["sleep_h"] - 7) + 0.6 * (eff - 88) + rng.gauss(0, 3), 35, 98))
        hrv_c = int(clamp(76 + 12 * s["hrv_z"] + rng.gauss(0, 4), 1, 100))
        rhr_c = int(clamp(82 - 4 * (s["lowest"] - 56) + rng.gauss(0, 4), 1, 100))
        score = int(clamp(74 + 7 * s["hrv_z"] + 4 * (s["sleep_h"] - 7)
                          - 25 * max(s["temp_dev"] - 0.3, 0) + rng.gauss(0, 5), 30, 98))
        rd = {"id": rid(rng), "day": d.isoformat(), "score": score,
              "temperature_deviation": s["temp_dev"], "timestamp": iso(local(d, 0)),
              "contributors": {"hrv_balance": hrv_c, "resting_heart_rate": rhr_c,
                               "sleep_balance": int(clamp(sleep_score + rng.gauss(0, 5), 1, 100)),
                               "previous_night": sleep_score, "body_temperature":
                               int(clamp(95 - 60 * abs(s["temp_dev"]), 1, 100))}}
        R.append({"day": rd["day"], "score": score, "temperature_deviation": s["temp_dev"],
                  "resting_heart_rate_contrib": rhr_c, "hrv_balance_contrib": hrv_c, "raw_json": rd})
        dsl = {"id": rid(rng), "day": d.isoformat(), "score": sleep_score,
               "contributors": {"total_sleep": int(clamp(50 + 12 * (s["sleep_h"] - 6), 1, 100)),
                                "efficiency": eff}}
        DS.append({"day": dsl["day"], "score": sleep_score, "raw_json": dsl})

        # Awake resting HR every 5 min with orthostatic spikes.
        end_awake = now if is_today else local(d + timedelta(days=1), -1)
        busy = []  # workout windows (aware) to skip resting samples
        workouts = plan_workouts(rng, s, is_today)
        cls = class_for(d, xrng, force=is_today)
        if cls and cls[1] + timedelta(minutes=cls[2]) > now:
            cls = None                                        # hasn't happened yet: lives in the calendar
        if cls:
            c0, c1 = cls[1] - timedelta(minutes=60), cls[1] + timedelta(minutes=cls[2] + 30)
            workouts = [w for w in workouts if not (w[1] < c1 and w[1] + timedelta(minutes=w[2]) > c0)]
        if d in padel_days and not is_today:
            pst = local(d, 19, xrng.choice([0, 0, 15, 30]))
            workouts = [w for w in workouts if abs((w[1] - pst).total_seconds()) > 3 * 3600]
            workouts.append(("padel", pst, xrng.choice([75, 80, 90])))
        for act, st, mins in workouts:
            busy.append((st, st + timedelta(minutes=mins + 30)))
        if cls:
            busy.append((cls[1] - timedelta(minutes=25), cls[1] + timedelta(minutes=cls[2])))
        t = wake
        while t < end_awake:
            spike = rng.uniform(18, 38) if rng.random() < 0.15 else 0
            noise = rng.gauss(0, 4)
            if not any(a <= t < b for a, b in busy):
                add_hr(t, rest_hr + spike + noise, "awake")
            t += timedelta(minutes=5)

        # Class (a daily-kind activity from the calendar): walk to campus first, then sparse seated HR.
        if cls:
            title, cst, cmins, cloc = cls
            k = 0
            while k < 20:                                     # ~20-min walk to campus, not logged as a workout
                wt = cst - timedelta(minutes=22 - k)
                add_hr(wt, 99 + 7 * clamp(s["strain"], -1.5, 3) + 0.3 * k + xrng.gauss(0, 4), "awake")
                k += xrng.choice([2, 3, 4])
            for off, bpm in class_hr(xrng, cmins, s, rest_hr):
                add_hr(cst + timedelta(minutes=off), bpm, "awake")
            cen = cst + timedelta(minutes=cmins)
            ev = {"id": "hist-%s-class" % cst.strftime("%Y%m%d%H%M"), "calendar_id": "primary",
                  "title": title, "start": iso(cst), "end": iso(cen), "location": cloc}
            CE.append(dict(ev, raw_json=dict(ev)))
        if not is_today and d.weekday() in (1, 4) and xrng.random() < 0.5:   # untracked history events
            cst = local(d, 15, 30)
            ev = {"id": "hist-%s-coffee" % cst.strftime("%Y%m%d%H%M"), "calendar_id": "primary",
                  "title": "Coffee chat", "start": iso(cst), "end": iso(cst + timedelta(minutes=45)),
                  "location": "Blue Bottle"}
            CE.append(dict(ev, raw_json=dict(ev)))

        # Workouts.
        walk_steps = 0
        for act, st, mins in workouts:
            hours = st.hour + st.minute / 60.0
            ws = dict(s)
            if 11 <= hours < 16 and s["hot"]:
                ws["strain"] += 0.6                           # midday heat
            if act == "walking":
                series = walk_hr(rng, mins, ws, hours)
            elif act == "padel":
                series = padel_hr(xrng, mins, ws)
            else:
                series = pilates_hr(rng, mins, ws)
            for i, bpm in enumerate(series):
                add_hr(st + timedelta(seconds=30 * i), bpm, "workout")
            for i, bpm in enumerate(recovery_hr(rng, series[-1], rest_hr, ws)):
                add_hr(st + timedelta(minutes=mins, seconds=30 * i), bpm, "awake")
            en = st + timedelta(minutes=mins)
            dist = round(mins * rng.uniform(70, 85), 1) if act == "walking" else None
            if act == "walking":
                walk_steps += int(mins * rng.uniform(95, 110))
            cal = round(mins * {"walking": 4.2, "padel": 6.5}.get(act, 3.6) * rng.uniform(0.9, 1.1), 1)
            w = {"id": rid(rng), "activity": act, "day": d.isoformat(),
                 "start_datetime": iso(st), "end_datetime": iso(en),
                 "intensity": rng.choice(["easy", "easy", "moderate"]), "calories": cal,
                 "distance": dist, "source": rng.choice(["manual", "autodetected", "confirmed"]),
                 "label": None}
            W.append(dict(w, raw_json=dict(w)))

        # Stress (seconds) and activity (seconds / steps). Today: partial day.
        frac = 1.0
        if is_today:
            frac = clamp((now_local.hour + now_local.minute / 60.0 - 7) / 15.0, 0.05, 1)
        sh = int(clamp(s["stress"] * 5 * 3600 * frac + rng.gauss(0, 600), 0, 10 * 3600)) // 60 * 60
        rh = int(clamp((1 - s["stress"]) * 3 * 3600 * frac + rng.gauss(0, 600), 0, 8 * 3600)) // 60 * 60
        summ = "stressful" if sh > 3 * 3600 else ("restored" if rh > sh else "normal")
        if is_today and frac < 0.6:
            summ = None                                       # Oura has no summary yet
        sd = {"id": rid(rng), "day": d.isoformat(), "stress_high": sh, "recovery_high": rh,
              "day_summary": summ}
        ST.append(dict(sd, raw_json=dict(sd)))
        steps = int((rng.gauss(3800, 900) + walk_steps) * frac)
        low = int(steps * 0.35)
        med = int(walk_steps * 0.6)
        da = {"id": rid(rng), "day": d.isoformat(), "score": int(clamp(60 + steps / 300 + rng.gauss(0, 5), 20, 99)),
              "steps": steps, "active_calories": int(steps * 0.04),
              "high_activity_time": 0, "medium_activity_time": med, "low_activity_time": low}
        DA.append(dict(da, raw_json=dict(da)))

    hr = [{"timestamp": k, "bpm": v[0], "source": v[1]} for k, v in sorted(hr_rows.items())]
    db.upsert_many(conn, "workouts", [{k: r[k] for k in r if k != "time_in_bed"} for r in W])
    db.upsert_many(conn, "daily_readiness", R)
    db.upsert_many(conn, "daily_sleep", DS)
    db.upsert_many(conn, "sleep", [{k: r[k] for k in r if k != "time_in_bed"} for r in SL])
    db.upsert_many(conn, "daily_stress", [{k: r[k] for k in r if k != "id"} for r in ST])
    db.upsert_many(conn, "daily_activity", [{k: r[k] for k in r if k != "id"} for r in DA])
    db.upsert_many(conn, "heartrate", hr)
    conn.execute("CREATE TABLE IF NOT EXISTS calendar_events(id TEXT PRIMARY KEY, calendar_id TEXT, title TEXT, "
                 "start TEXT, end TEXT, location TEXT, raw_json TEXT)")
    db.upsert_many(conn, "calendar_events", CE)
    sync_at = iso(now_local - timedelta(minutes=rng.randint(15, 50)))  # pretend last sync a bit ago
    for ep in ["workout", "daily_readiness", "daily_sleep", "sleep", "daily_stress",
               "daily_activity", "heartrate"]:
        db.set_sync_state(conn, ep, sync_at, today.isoformat())
    conn.commit()
    result = db.counts(conn)
    conn.close()

    write_calendar(rng, now_local, Path(cal_path) if cal_path else db.MOCK_CALENDAR, demo=demo_calendar)
    return result


# ---- mock calendar --------------------------------------------------------------
def write_demo_calendar(now_local, path):
    """Fixed-time demo calendar (SPEC addenda v2/v3 "Static demo"), today + 6 days, deterministic.

    Today: BA780 class 9:30, Solidcore 17:00, walk 19:00. Tomorrow: morning walk, MSBA seminar 13:00,
    padel 19:00. Untracked: lunch, a call and "Hot yoga w/ Deniz" (effortful but not a tracked activity).
    """
    today = now_local.date()
    ev = []

    def add(title, d, hour, minute, minutes, location=""):
        st = local(d, hour, minute)
        ev.append({"id": "demo-%s-%02d" % (st.strftime("%Y%m%d%H%M"), len(ev)), "title": title,
                   "start": iso(st), "end": iso(st + timedelta(minutes=minutes)), "location": location})

    # today (fixed "now" 15:30): class 9:30 (done), Solidcore ~17:00, walk ~19:00, plus untracked events
    add("BA780 class", today, 9, 30, 90, "Hall B 204")
    add("Lunch with Priya", today, 12, 0, 45, "Tatte")
    add("Solidcore \u2014 Full Body", today, 17, 0, 50, "[solidcore] Back Bay")
    add("Evening walk", today, 19, 0, 30, "Riverside path")
    add("Call with Mom", today, 20, 15, 30, "")
    # tomorrow: morning walk, class 13:00, padel 19:00 (+ an unrecognised, effortful event)
    tom = today + timedelta(days=1)
    add("Morning walk", tom, 7, 45, 30, "Neighborhood loop")
    add("MSBA seminar", tom, 13, 0, 90, "Hall C 110")
    add("Hot yoga w/ Deniz", tom, 16, 0, 60, "Inferno Hot Yoga")
    add("Padel w/ Ana", tom, 19, 0, 90, "Boston Padel \u2014 Court 3")
    titles = ["Solidcore \u2014 Full Body", "[solidcore] Arms + Abs", "[solidcore] Legs + Glutes"]
    for i in range(2, 7):
        d = today + timedelta(days=i)
        add("Morning walk", d, 7, 30, (25, 30, 40)[i % 3], "Neighborhood loop")
        c = CLASS_SCHEDULE.get(d.weekday())
        if c:
            add(c[0], d, c[1], c[2], c[3], c[4])
        if d.weekday() in (1, 4, 5):
            add(titles[i % 3], d, 10 if d.weekday() != 4 else 17, 0, 50, "[solidcore] Back Bay")
        if d.weekday() == 5:
            add("Padel w/ Ana", d, 18, 0, 90, "Boston Padel \u2014 Court 3")
        if d.weekday() == 3:
            add("Evening walk", d, 18, 30, 35, "Riverside path")
    ev.sort(key=lambda e: e["start"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ev, indent=2, ensure_ascii=False))
    return ev


def write_calendar(rng, now_local, path, demo=False):
    """Events for today + 6 days. One walk starts ~75 min from now (watcher demo)."""
    if demo:
        return write_demo_calendar(now_local, path)
    today = now_local.date()
    ev = []

    def add(title, start, minutes, location=""):
        ev.append({"id": "mock-%s-%02d" % (start.strftime("%Y%m%d%H%M"), len(ev)), "title": title,
                   "start": iso(start), "end": iso(start + timedelta(minutes=minutes)),
                   "location": location})

    rounded = now_local.replace(second=0, microsecond=0)
    rounded -= timedelta(minutes=rounded.minute % 5)
    add("Coffee chat", rounded + timedelta(minutes=20), 30, "Blue Bottle")
    add("Walk to campus", rounded + timedelta(minutes=75), 30, "")
    add("Solidcore", rounded + timedelta(minutes=75 + 180), 50, "[solidcore] Back Bay")
    add("MSBA class", rounded + timedelta(minutes=75 + 60), 90, "Hall B 204")

    for i in range(1, 7):
        d = today + timedelta(days=i)
        add("Morning walk", local(d, 7, 30), rng.choice([25, 30, 40]), "Neighborhood loop")
        if d.weekday() in (1, 4):
            add("Pilates w/ Sara", local(d, 10), 50, "Core Studio")
        elif d.weekday() == 5:
            add("Solidcore", local(d, 11), 50, "[solidcore] Back Bay")
        if d.weekday() < 5:
            add("MSBA class", local(d, 13), 90, "Hall B 204")
            add("Walk to campus", local(d, 12, 25), 25, "")
        if d.weekday() in (2, 6):
            add("Coffee chat", local(d, 16), 45, "")
        if d.weekday() == 3:
            add("Evening stroll", local(d, 18, 30), 35, "Riverside path")
    ev.sort(key=lambda e: e["start"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ev, indent=2))
    return ev


def main(argv=None):
    ap = argparse.ArgumentParser(description="Create data/mock.db + data/mock_calendar.json")
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--today", choices=sorted(TODAY_PRESETS), default="meh",
                    help="body state for today (default: meh = a bit below usual)")
    ap.add_argument("--demo-calendar", action="store_true",
                    help="fixed demo events (today: Solidcore 17:00, walk 19:00; tomorrow: walk + Solidcore)")
    ap.add_argument("--now", help="pretend 'now' (ISO date-time; naive = New York time)")
    ap.add_argument("--db", help="output DB path (default data/mock.db)")
    ap.add_argument("--calendar", help="output calendar path (default data/mock_calendar.json)")
    args = ap.parse_args(argv)
    now = None
    if args.now:
        now = datetime.fromisoformat(args.now)
        if now.tzinfo is None:
            now = now.replace(tzinfo=ny_tz(now))
    db_path = Path(args.db) if args.db else db.MOCK_DB
    cal_path = Path(args.calendar) if args.calendar else db.MOCK_CALENDAR
    counts = generate(days=args.days, seed=args.seed, today_preset=args.today, db_path=db_path,
                      cal_path=cal_path, now=now, demo_calendar=args.demo_calendar)
    print("Mock data written to %s" % db_path)
    for t, n in counts.items():
        print("   %-16s %d" % (t, n))
    print("Mock calendar written to %s" % cal_path)


if __name__ == "__main__":
    main()
