"""Data-layer self-test: build mock.db, print row counts and a per-session sanity summary.

    python3 tests/selftest_data.py
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from planwell import db, mock_data  # noqa: E402


def utc(s):
    return datetime.fromisoformat(s).astimezone(timezone.utc).isoformat()


def main():
    counts = mock_data.generate(days=180, seed=7)
    print("Row counts:", json.dumps(counts))
    conn = db.connect(db.MOCK_DB)
    sessions = []
    for w in conn.execute("SELECT w.*, r.score readiness, s.average_hrv hrv, s.total_sleep_duration tsd "
                          "FROM workouts w LEFT JOIN daily_readiness r ON r.day=w.day "
                          "LEFT JOIN sleep s ON s.day=w.day AND s.type='long_sleep'"):
        hr = conn.execute("SELECT bpm FROM heartrate WHERE timestamp>=? AND timestamp<? ",
                          (utc(w["start_datetime"]), utc(w["end_datetime"]))).fetchall()
        dur = (datetime.fromisoformat(w["end_datetime"]) - datetime.fromisoformat(w["start_datetime"])).seconds / 60
        above = sum(1 for r in hr if r[0] > 125) * 0.5          # 30-s samples -> minutes
        sessions.append((w["activity"], w["readiness"], w["hrv"], w["tsd"] / 3600, dur,
                         above, above / dur * 30, max(r[0] for r in hr)))
    for act in ("walking", "pilates"):
        ss = sorted(s for s in sessions if s[0] == act)
        per30 = sorted(s[6] for s in ss)
        n = len(per30)
        print("\n%s: %d sessions; min>125 per 30 min: p10=%.1f median=%.1f p90=%.1f"
              % (act, n, per30[n // 10], per30[n // 2], per30[9 * n // 10]))
        lo = [s[6] for s in ss if s[2] < 38]
        hi = [s[6] for s in ss if s[2] >= 50]
        print("   low-HRV(<38) days mean %.1f (n=%d)  vs  high-HRV(>=50) days mean %.1f (n=%d)"
              % (sum(lo) / max(len(lo), 1), len(lo), sum(hi) / max(len(hi), 1), len(hi)))
    today = mock_data.to_ny(datetime.now(timezone.utc)).date().isoformat()
    print("\nToday", today, dict(conn.execute("SELECT score, temperature_deviation FROM daily_readiness WHERE day=?", (today,)).fetchone()))
    print("Latest HR sample:", tuple(conn.execute("SELECT * FROM heartrate ORDER BY timestamp DESC LIMIT 1").fetchone()))
    print("Weekly rate: walks %.1f, pilates %.1f" % tuple(
        sum(1 for s in sessions if s[0] == a) / (180 / 7.0) for a in ("walking", "pilates")))
    cal = json.loads(db.MOCK_CALENDAR.read_text())
    print("Calendar: %d events; first today: %s" % (len(cal), [(e["title"], e["start"]) for e in cal[:4]]))
    conn.close()


if __name__ == "__main__":
    main()
