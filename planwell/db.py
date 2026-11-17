"""SQLite schema and small helpers shared by sync, mock_data, engine and watcher.

The schema is identical for the real DB (data/planwell.db) and the mock DB
(data/mock.db). All timestamps are ISO-8601 strings with offset, exactly as
Oura returns them; `day` is YYYY-MM-DD.
"""
import json
import os
import sqlite3
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
REAL_DB = DATA_DIR / "planwell.db"
MOCK_DB = DATA_DIR / "mock.db"
MOCK_CALENDAR = DATA_DIR / "mock_calendar.json"

SCHEMA = """
CREATE TABLE IF NOT EXISTS workouts(
    id TEXT PRIMARY KEY, activity TEXT, day TEXT, start_datetime TEXT, end_datetime TEXT,
    intensity TEXT, calories REAL, distance REAL, source TEXT, label TEXT, raw_json TEXT);
CREATE TABLE IF NOT EXISTS heartrate(
    timestamp TEXT PRIMARY KEY, bpm INTEGER, source TEXT);
CREATE TABLE IF NOT EXISTS daily_readiness(
    day TEXT PRIMARY KEY, score INTEGER, temperature_deviation REAL,
    resting_heart_rate_contrib INTEGER, hrv_balance_contrib INTEGER, raw_json TEXT);
CREATE TABLE IF NOT EXISTS daily_sleep(
    day TEXT PRIMARY KEY, score INTEGER, raw_json TEXT);
CREATE TABLE IF NOT EXISTS sleep(
    id TEXT PRIMARY KEY, day TEXT, type TEXT, bedtime_start TEXT, bedtime_end TEXT,
    total_sleep_duration INTEGER, average_hrv REAL, average_heart_rate REAL,
    lowest_heart_rate INTEGER, efficiency INTEGER, raw_json TEXT);
CREATE TABLE IF NOT EXISTS daily_stress(
    day TEXT PRIMARY KEY, stress_high INTEGER, recovery_high INTEGER, day_summary TEXT, raw_json TEXT);
CREATE TABLE IF NOT EXISTS daily_activity(
    day TEXT PRIMARY KEY, score INTEGER, steps INTEGER, active_calories INTEGER,
    high_activity_time INTEGER, medium_activity_time INTEGER, low_activity_time INTEGER, raw_json TEXT);
CREATE TABLE IF NOT EXISTS sync_state(
    endpoint TEXT PRIMARY KEY, last_synced_at TEXT, last_day TEXT);
CREATE TABLE IF NOT EXISTS notified(
    event_id TEXT PRIMARY KEY, notified_at TEXT, verdict TEXT);
CREATE TABLE IF NOT EXISTS calendar_events(id TEXT PRIMARY KEY, calendar_id TEXT, title TEXT, start TEXT, end TEXT, location TEXT, raw_json TEXT);
CREATE TABLE IF NOT EXISTS event_labels(key TEXT PRIMARY KEY, label_json TEXT, created_at TEXT);
CREATE INDEX IF NOT EXISTS idx_workouts_day ON workouts(day);
CREATE INDEX IF NOT EXISTS idx_workouts_activity ON workouts(activity);
CREATE INDEX IF NOT EXISTS idx_sleep_day ON sleep(day);
CREATE INDEX IF NOT EXISTS idx_calendar_events_start ON calendar_events(start);
"""

TABLES = ["workouts", "heartrate", "daily_readiness", "daily_sleep", "sleep",
          "daily_stress", "daily_activity", "sync_state", "notified",
          "calendar_events", "event_labels"]


def default_path(mock=False):
    """Path of the real or mock database."""
    return MOCK_DB if mock else REAL_DB


def _private(p, mode):
    try:
        if p.exists():
            os.chmod(str(p), mode)
    except OSError:
        pass


def connect(path=None):
    """Open (creating if needed) a PlanWell DB. Rows behave like dicts (sqlite3.Row)."""
    path = Path(path) if path else REAL_DB
    path.parent.mkdir(parents=True, exist_ok=True)
    old_umask = os.umask(0o077)  # health data: new DB/WAL files readable by this user only
    try:
        conn = sqlite3.connect(str(path))
        conn.execute("PRAGMA journal_mode=WAL")
    finally:
        os.umask(old_umask)
    _private(path.parent, 0o700)
    for suffix in ("", "-wal", "-shm"):
        _private(Path(str(path) + suffix), 0o600)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def upsert(conn, table, row):
    """INSERT OR REPLACE one dict row. A dict/list under 'raw_json' is serialised."""
    upsert_many(conn, table, [row])


def upsert_many(conn, table, rows):
    """INSERT OR REPLACE many dict rows (all with the same keys). Returns count."""
    rows = list(rows)
    if not rows:
        return 0
    if table not in TABLES:
        raise ValueError("unknown table: %s" % table)
    cols = list(rows[0].keys())
    sql = "INSERT OR REPLACE INTO %s (%s) VALUES (%s)" % (
        table, ", ".join(cols), ", ".join("?" for _ in cols))
    values = []
    for r in rows:
        vals = []
        for c in cols:
            v = r.get(c)
            if c == "raw_json" and not isinstance(v, (str, type(None))):
                v = json.dumps(v, separators=(",", ":"))
            vals.append(v)
        values.append(vals)
    conn.executemany(sql, values)
    return len(values)


def get_sync_state(conn, endpoint):
    row = conn.execute("SELECT * FROM sync_state WHERE endpoint=?", (endpoint,)).fetchone()
    return dict(row) if row else None


def set_sync_state(conn, endpoint, last_synced_at, last_day):
    upsert(conn, "sync_state", {"endpoint": endpoint, "last_synced_at": last_synced_at,
                                "last_day": last_day})


def was_notified(conn, event_id):
    return conn.execute("SELECT 1 FROM notified WHERE event_id=?", (event_id,)).fetchone() is not None


def mark_notified(conn, event_id, notified_at, verdict):
    upsert(conn, "notified", {"event_id": event_id, "notified_at": notified_at, "verdict": verdict})
    conn.commit()


def counts(conn):
    """{table: row_count} for every table."""
    return {t: conn.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0] for t in TABLES}
