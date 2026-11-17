"""Small time helpers shared by engine, gcal, watcher and server (stdlib only, 3.9+)."""
import re
from datetime import date, datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except Exception:  # pragma: no cover
    ZoneInfo = None

_FRAC = re.compile(r"^(.*T\d\d:\d\d(?::\d\d)?)(\.\d+)?(.*)$")


def get_tz(config=None):
    """tzinfo for config['timezone'], falling back to the system local zone."""
    name = (config or {}).get("timezone") if isinstance(config, dict) else None
    if name and ZoneInfo is not None:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    return datetime.now().astimezone().tzinfo


def parse_iso(value, tz=None):
    """Parse an ISO-8601 string (with 'Z', any fractional digits, or naive) to an aware datetime.

    Naive values are interpreted in `tz` (default UTC). Returns None for empty/invalid input.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime(value.year, value.month, value.day)
    else:
        s = str(value).strip()
        if s.endswith("Z") or s.endswith("z"):
            s = s[:-1] + "+00:00"
        m = _FRAC.match(s)
        if m and m.group(2):
            frac = (m.group(2)[1:] + "000000")[:6]
            s = m.group(1) + "." + frac + m.group(3)
        # Python 3.9 fromisoformat dislikes offsets like +0000
        s = re.sub(r"([+-]\d\d)(\d\d)$", r"\1:\2", s)
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            try:
                d = date.fromisoformat(s[:10])
                dt = datetime(d.year, d.month, d.day)
            except ValueError:
                return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz or timezone.utc)
    return dt


def iso(dt):
    """Aware datetime -> ISO string with offset, second precision."""
    if dt is None:
        return None
    return dt.replace(microsecond=0).isoformat()


def utc_str(dt):
    """Aware datetime -> 'YYYY-MM-DDTHH:MM:SS' in UTC (for string-range queries)."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def now_in(config=None, now=None):
    tz = get_tz(config)
    if now is None:
        return datetime.now(tz)
    if isinstance(now, str):
        now = parse_iso(now, tz)
    if now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    return now.astimezone(tz)


def day_str(d):
    return d.isoformat() if isinstance(d, date) and not isinstance(d, datetime) else d.date().isoformat()


def add_days(day, n):
    return (date.fromisoformat(day) + timedelta(days=n)).isoformat()


def fmt_clock(dt):
    """'3:05 PM' without a leading zero (portable)."""
    h = dt.hour % 12 or 12
    return "%d:%02d %s" % (h, dt.minute, "AM" if dt.hour < 12 else "PM")


def fmt_hm(minutes):
    """340 -> '5h 40m'."""
    minutes = int(round(minutes))
    h, m = divmod(minutes, 60)
    if h and m:
        return "%dh %02dm" % (h, m)
    if h:
        return "%dh" % h
    return "%dm" % m
