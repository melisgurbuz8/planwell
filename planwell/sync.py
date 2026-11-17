"""Incremental pull of Oura data into data/planwell.db.

    python3 -m planwell.sync               # incremental (first run: last 365 days)
    python3 -m planwell.sync --days 365    # (re)pull a fixed window
    python3 -m planwell.sync --recent      # last 2 days only (fast; used by the watcher)

Each incremental run re-pulls the last 3 days, because Oura finalises data late.
Heartrate timestamps are stored normalised to UTC ("YYYY-MM-DDTHH:MM:SS+00:00"),
which is also what Oura returns, so range queries can compare strings.
"""
import argparse
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone

from planwell import db
from planwell.oura_client import OuraAPIError, OuraAuthError, OuraClient

DEFAULT_DAYS = 365
OVERLAP_DAYS = 3
RECENT_DAYS = 2
HR_CHUNK_DAYS = 30

DAILY_ENDPOINTS = ["workout", "daily_readiness", "daily_sleep", "sleep",
                   "daily_stress", "daily_activity"]


def utc_iso(ts):
    """Normalise an ISO timestamp (any offset, or 'Z') to 'YYYY-MM-DDTHH:MM:SS+00:00'."""
    if ts is None:
        return None
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


# ---- field mapping: Oura JSON -> table rows ------------------------------
def map_workout(d):
    return {"id": d.get("id"), "activity": d.get("activity"), "day": d.get("day"),
            "start_datetime": d.get("start_datetime"), "end_datetime": d.get("end_datetime"),
            "intensity": d.get("intensity"), "calories": d.get("calories"),
            "distance": d.get("distance"), "source": d.get("source"), "label": d.get("label"),
            "raw_json": d}


def map_readiness(d):
    c = d.get("contributors") or {}
    return {"day": d.get("day"), "score": d.get("score"),
            "temperature_deviation": d.get("temperature_deviation"),
            "resting_heart_rate_contrib": c.get("resting_heart_rate"),
            "hrv_balance_contrib": c.get("hrv_balance"), "raw_json": d}


def map_daily_sleep(d):
    return {"day": d.get("day"), "score": d.get("score"), "raw_json": d}


def map_sleep(d):
    return {"id": d.get("id"), "day": d.get("day"), "type": d.get("type"),
            "bedtime_start": d.get("bedtime_start"), "bedtime_end": d.get("bedtime_end"),
            "total_sleep_duration": d.get("total_sleep_duration"),
            "average_hrv": d.get("average_hrv"), "average_heart_rate": d.get("average_heart_rate"),
            "lowest_heart_rate": d.get("lowest_heart_rate"), "efficiency": d.get("efficiency"),
            "raw_json": d}


def map_stress(d):
    return {"day": d.get("day"), "stress_high": d.get("stress_high"),
            "recovery_high": d.get("recovery_high"), "day_summary": d.get("day_summary"),
            "raw_json": d}


def map_activity(d):
    return {"day": d.get("day"), "score": d.get("score"), "steps": d.get("steps"),
            "active_calories": d.get("active_calories"),
            "high_activity_time": d.get("high_activity_time"),
            "medium_activity_time": d.get("medium_activity_time"),
            "low_activity_time": d.get("low_activity_time"), "raw_json": d}


# endpoint -> (table, mapper)
MAPPING = {
    "workout": ("workouts", map_workout),
    "daily_readiness": ("daily_readiness", map_readiness),
    "daily_sleep": ("daily_sleep", map_daily_sleep),
    "sleep": ("sleep", map_sleep),
    "daily_stress": ("daily_stress", map_stress),
    "daily_activity": ("daily_activity", map_activity),
}


def _start_day(conn, endpoint, today, days, recent):
    """Where to start pulling for this endpoint."""
    if recent:
        return today - timedelta(days=RECENT_DAYS)
    if days is not None:
        return today - timedelta(days=days)
    st = db.get_sync_state(conn, endpoint)
    if st and st.get("last_day"):
        last = date.fromisoformat(st["last_day"])
        return min(last, today) - timedelta(days=OVERLAP_DAYS)
    return today - timedelta(days=DEFAULT_DAYS)


def sync(days=None, recent=False, db_path=None, client=None, today=None, log=None):
    """Pull Oura data into the DB. Returns a summary dict:
    {"ok": bool, "rows": {endpoint: n_rows_upserted}, "workouts_by_activity": {...},
     "last_synced_at": ISO, "error": str|None}
    Never raises for auth/API problems (the watcher keeps working on cached data);
    the message is in "error".
    """
    log = log or (lambda msg: None)
    today = today or date.today()
    now_iso = datetime.now(timezone.utc).astimezone().replace(microsecond=0).isoformat()
    result = {"ok": True, "rows": {}, "workouts_by_activity": {}, "last_synced_at": now_iso,
              "error": None}
    conn = db.connect(db_path or db.REAL_DB)
    try:
        client = client or OuraClient()
        # Oura's end_date is treated as exclusive by some endpoints -> ask for tomorrow.
        end = (today + timedelta(days=1)).isoformat()
        for ep in DAILY_ENDPOINTS:
            start = _start_day(conn, ep, today, days, recent)
            log("Fetching %s from %s ..." % (ep, start))
            data = client.get_all(ep, start_date=start.isoformat(), end_date=end)
            table, mapper = MAPPING[ep]
            rows = [mapper(d) for d in data if d.get("day") or d.get("id")]
            db.upsert_many(conn, table, rows)
            if ep == "workout":
                result["workouts_by_activity"] = dict(Counter(r["activity"] for r in rows))
            last_day = max([r["day"] for r in rows if r.get("day")] or [start.isoformat()])
            db.set_sync_state(conn, ep, now_iso, last_day)
            conn.commit()
            result["rows"][ep] = len(rows)

        # Heartrate: datetime range, chunked to <= 30 days.
        start = _start_day(conn, "heartrate", today, days, recent)
        start_dt = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
        end_dt = datetime.now(timezone.utc).replace(microsecond=0)
        n_hr, latest = 0, None
        while start_dt < end_dt:
            chunk_end = min(start_dt + timedelta(days=HR_CHUNK_DAYS), end_dt)
            log("Fetching heartrate %s .. %s ..." % (start_dt.date(), chunk_end.date()))
            data = client.get_all("heartrate", start_datetime=start_dt.isoformat(),
                                  end_datetime=chunk_end.isoformat())
            rows = [{"timestamp": utc_iso(d["timestamp"]), "bpm": d.get("bpm"),
                     "source": d.get("source")} for d in data if d.get("timestamp")]
            db.upsert_many(conn, "heartrate", rows)
            conn.commit()
            n_hr += len(rows)
            if rows:
                latest = max(latest or "", max(r["timestamp"] for r in rows))
            start_dt = chunk_end
        last_day = latest[:10] if latest else start.isoformat()
        db.set_sync_state(conn, "heartrate", now_iso, last_day)
        conn.commit()
        result["rows"]["heartrate"] = n_hr
    except (OuraAuthError, OuraAPIError) as e:
        result["ok"] = False
        result["error"] = str(e)
    finally:
        result["table_counts"] = db.counts(conn)
        conn.close()
    return result


def main(argv=None):
    ap = argparse.ArgumentParser(description="Pull your Oura data into data/planwell.db")
    ap.add_argument("--days", type=int, default=None,
                    help="pull this many past days (default: incremental, 365 on first run)")
    ap.add_argument("--recent", action="store_true", help="only the last 2 days (fast)")
    args = ap.parse_args(argv)

    res = sync(days=args.days, recent=args.recent, log=print)
    if not res["ok"]:
        print("\nSync stopped: " + res["error"])
        return 1
    print("\nSync complete. New/updated rows this run:")
    for ep, n in res["rows"].items():
        print("   %-16s %d" % (ep, n))
    print("Rows now stored:")
    for t, n in res["table_counts"].items():
        if t not in ("sync_state", "notified"):
            print("   %-16s %d" % (t, n))
    if res["workouts_by_activity"]:
        print("Workouts pulled this run by activity:")
        for a, n in sorted(res["workouts_by_activity"].items(), key=lambda x: -x[1]):
            print("   %-16s %d" % (a, n))
    return 0


if __name__ == "__main__":
    sys.exit(main())
