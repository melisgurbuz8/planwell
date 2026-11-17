"""List your Google calendars and next 7 days of event titles, to set up PlanWell keywords.

Run from the planwell folder:  python3 scripts/gcal_diagnose.py
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from planwell import gcal  # noqa: E402

cals = gcal._api_get("/users/me/calendarList", {}).get("items", [])
print("Your calendars (id -> name):")
for c in cals:
    print("  %-50s %s%s" % (c["id"], c.get("summary", ""), "  [primary]" if c.get("primary") else ""))

now = datetime.now(timezone.utc)
params = {"timeMin": now.isoformat(), "timeMax": (now + timedelta(days=7)).isoformat(),
          "singleEvents": "true", "orderBy": "startTime", "maxResults": 100}
for c in cals:
    items = gcal._api_get("/calendars/%s/events" % gcal.urllib.parse.quote(c["id"], safe=""), params).get("items", [])
    print("\n%s: %d events in the next 7 days" % (c.get("summary", c["id"]), len(items)))
    for e in items:
        st = e.get("start", {})
        print("   %s  %s" % ((st.get("dateTime") or st.get("date") or "")[:16], e.get("summary", "(no title)")))
