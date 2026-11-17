"""Small deterministic SQLite fixture with the exact SPEC schema (independent of mock_data.py).

Pattern: every 4th day is a "low" day (readiness 52, HRV -30%, RHR +7, short sleep) and sessions
on low days run hotter. Afternoon walks run hotter than morning walks (so "move earlier" fires).
"""
import json
import sqlite3
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None

TZ = ZoneInfo("America/New_York")
SCHEMA = """
CREATE TABLE workouts(id TEXT PRIMARY KEY, activity TEXT, day TEXT, start_datetime TEXT, end_datetime TEXT,
         intensity TEXT, calories REAL, distance REAL, source TEXT, label TEXT, raw_json TEXT);
CREATE TABLE heartrate(timestamp TEXT PRIMARY KEY, bpm INTEGER, source TEXT);
CREATE TABLE daily_readiness(day TEXT PRIMARY KEY, score INTEGER, temperature_deviation REAL,
                resting_heart_rate_contrib INTEGER, hrv_balance_contrib INTEGER, raw_json TEXT);
CREATE TABLE daily_sleep(day TEXT PRIMARY KEY, score INTEGER, raw_json TEXT);
CREATE TABLE sleep(id TEXT PRIMARY KEY, day TEXT, type TEXT, bedtime_start TEXT, bedtime_end TEXT,
      total_sleep_duration INTEGER, average_hrv REAL, average_heart_rate REAL,
      lowest_heart_rate INTEGER, efficiency INTEGER, raw_json TEXT);
CREATE TABLE daily_stress(day TEXT PRIMARY KEY, stress_high INTEGER, recovery_high INTEGER, day_summary TEXT, raw_json TEXT);
CREATE TABLE daily_activity(day TEXT PRIMARY KEY, score INTEGER, steps INTEGER, active_calories INTEGER,
               high_activity_time INTEGER, medium_activity_time INTEGER, low_activity_time INTEGER, raw_json TEXT);
CREATE TABLE sync_state(endpoint TEXT PRIMARY KEY, last_synced_at TEXT, last_day TEXT);
CREATE TABLE calendar_events(id TEXT PRIMARY KEY, calendar_id TEXT, title TEXT, start TEXT, end TEXT, location TEXT, raw_json TEXT);
"""

# "today" for the fixture
TODAY = datetime(2026, 2, 20, tzinfo=TZ).date()


def utc(dt):
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def is_low(i):
    return i % 4 == 0


def local(day, h, m=0):
    return datetime(day.year, day.month, day.day, h, m, tzinfo=TZ)


def build(path, days=40, today_low=True, workouts=True, max_workouts=None, latest_hr_at=None,
          padel=0, classes=False):
    """padel: number of past padel workouts (cold start); classes: past 'BA780 class' calendar events
    every other day 11:00-12:15 with 5-min heart rate, plus one class with no heart rate (skipped)."""
    conn = sqlite3.connect(str(path))
    conn.executescript(SCHEMA)
    hr = {}
    n_w = 0
    for back in range(days, -1, -1):
        day = TODAY - timedelta(days=back)
        ds = day.isoformat()
        low = today_low if back == 0 else is_low(back)
        conn.execute("INSERT INTO daily_readiness VALUES (?,?,?,?,?,?)",
                     (ds, 52 if low else 80, 0.6 if low else 0.0, 50, 50, "{}"))
        conn.execute("INSERT INTO daily_sleep VALUES (?,?,?)", (ds, 58 if low else 82, "{}"))
        conn.execute("INSERT INTO sleep VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     ("s" + ds, ds, "long_sleep", None, None, (5 * 3600 + 1800) if low else 7 * 3600 + 1800,
                      35.0 if low else 50.0, 60.0, 62 if low else 55, 85, "{}"))
        conn.execute("INSERT INTO sleep VALUES (?,?,?,?,?,?,?,?,?,?,?)",  # a nap that must be ignored
                     ("n" + ds, ds, "sleep", None, None, 1200, 10.0, 70.0, 80, 80, "{}"))
        conn.execute("INSERT INTO daily_stress VALUES (?,?,?,?,?)",
                     (ds, 7200 if low else 1800, 3600, "stressful" if low else "normal", "{}"))
        conn.execute("INSERT INTO daily_activity VALUES (?,?,?,?,?,?,?,?)",
                     (ds, 70, 3000 if back == 0 else 7000, 200, 0, 0, 0, "{}"))
        if classes and back > 0 and back % 2 == 0:
            st = local(day, 11)
            en = st + timedelta(minutes=75)
            conn.execute("INSERT INTO calendar_events VALUES (?,?,?,?,?,?,?)",
                         ("cls-%s" % ds, "primary", "BA780 class", st.isoformat(), en.isoformat(), "Hall B", "{}"))
            for k in range(0, 75 + 30, 5):  # sparse daytime HR, every 5 min
                hr[utc(st + timedelta(minutes=k))] = (96 if not low else 104) + (k % 3)
        if classes and back == 3:
            st = local(day, 11)
            conn.execute("INSERT INTO calendar_events VALUES (?,?,?,?,?,?,?)",
                         ("cls-nohr", "primary", "MSBA class", st.isoformat(),
                          (st + timedelta(minutes=60)).isoformat(), "", "{}"))
        if padel and 0 < back <= padel * 3 and back % 3 == 2:
            st = local(day, 19)
            base = 128 + (10 if low else 0)
            wid = "w-padel-%s" % ds
            en = st + timedelta(minutes=60)
            conn.execute("INSERT INTO workouts VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (wid, "padel", ds, st.isoformat(), en.isoformat(), "moderate", 300, 0,
                          "autodetected", None, "{}"))
            for k in range(120):
                hr[utc(st + timedelta(seconds=30 * k))] = base + (k % 8) - 4
        if back == 0 or not workouts:
            continue
        sess = []
        afternoon = back % 2 == 1
        sess.append(("walking", local(day, 17 if afternoon else 9), 30,
                     (124 if afternoon else 104) + (16 if low else 0)))
        if back % 3 == 0:
            sess.append(("pilates", local(day, 18, 30), 50, 112 + (18 if low else 0)))
        for act, st, dur, base in sess:
            if max_workouts is not None and n_w >= max_workouts:
                break
            n_w += 1
            wid = "w-%s-%s" % (act, ds)
            en = st + timedelta(minutes=dur)
            conn.execute("INSERT INTO workouts VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (wid, act, ds, st.isoformat(), en.isoformat(), "moderate", 150, 2000,
                          "manual", None, json.dumps({"id": wid})))
            for k in range(dur * 2):  # every 30 s
                t = st + timedelta(seconds=30 * k)
                ramp = min(1.0, k / 20.0)
                hr[utc(t)] = int(round(85 + (base - 85) * ramp + (k % 6)))
            for k in range(1, 31):  # recovery
                t = en + timedelta(minutes=k)
                hr[utc(t)] = max(80, base - 3 * k)
    if latest_hr_at is not None:
        hr[utc(latest_hr_at)] = 80
    conn.executemany("INSERT OR REPLACE INTO heartrate VALUES (?,?,?)",
                     [(t, b, "workout") for t, b in hr.items()])
    conn.execute("INSERT INTO sync_state VALUES (?,?,?)",
                 ("heartrate", local(TODAY, 14).isoformat(), TODAY.isoformat()))
    conn.commit()
    conn.close()
    return path
