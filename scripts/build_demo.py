"""Build the static demo (web/sample/) from REAL ENGINE OUTPUT on mock data.

    python3 scripts/build_demo.py [--date YYYY-MM-DD] [--seed 7] [--keep-tmp]

For each scenario (good | meh | poor) this regenerates a mock DB + demo calendar in a temp folder
(never data/mock.db), freezes "now" at the Wednesday before the build date, 15:30 New York time,
and calls the very same server handlers the live app uses (planwell.server.App in mock mode), so
every JSON file has exactly the live API shape.

Writes:
    web/sample/scenarios/<scenario>/{today,tomorrow,next48,check_walking,check_pilates}.json
    web/sample/{sessions,insights,backtest,settings}.json, web/sample/session_<id>.json
Shared files come from the "meh" scenario (history is identical in all three; only today differs).
Old hand-written sample files are removed. Uses mock data only (Oura data never touches this).
Demo settings = defaults + the activity registry with an example 12 h late-cancel window for
Solidcore, and the rules-only labeller (provider "none": labels carry source "rules").
"""
import argparse
import contextlib
import json
import os
import shutil
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from planwell import analytics, engine, gcal, mock_data, server, timeutil  # noqa: E402
from planwell import settings as settings_mod  # noqa: E402

SCENARIOS = ("good", "meh", "poor")
SHARED_FROM = "meh"
SAMPLE_DIR = REPO / "web" / "sample"
NY = "America/New_York"


def demo_now(build_date=None):
    """The Wednesday strictly before `build_date` (default today), 15:30 America/New_York."""
    d = build_date or date.today()
    back = (d.weekday() - 2) % 7 or 7
    wed = d - timedelta(days=back)
    tz = timeutil.get_tz({"timezone": NY})
    return datetime(wed.year, wed.month, wed.day, 15, 30, tzinfo=tz)


@contextlib.contextmanager
def frozen_now(now):
    """Make every module the API path uses see `now` as the current time."""
    real = timeutil.now_in

    def fake(config=None, when=None):
        return real(config, when if when is not None else now)

    mods = [server, engine, analytics]
    saved = [(m, m.now_in) for m in mods]
    for m in mods:
        m.now_in = fake
    try:
        yield
    finally:
        for m, fn in saved:
            m.now_in = fn


class DemoApp(server.App):
    """server.App in mock mode, reading this scenario's calendar file instead of data/mock_calendar.json."""

    def __init__(self, calendar_path, **kw):
        server.App.__init__(self, mock=True, **kw)
        self.calendar_path = str(calendar_path)

    def events(self, config, now):
        # same window as App.events (now-6h .. end of tomorrow), same loader as gcal.get_events
        tz = timeutil.get_tz(config)
        day0 = now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
        start, end = now - timedelta(hours=6), day0 + timedelta(days=2)
        out = [e for e in gcal.load_mock_events(config, self.calendar_path)
               if timeutil.parse_iso(e["start"], tz) < end and timeutil.parse_iso(e["end"], tz) > start]
        out.sort(key=lambda e: timeutil.parse_iso(e["start"], tz))
        return out, True, None


def demo_config():
    """Defaults + registry; example 12 h late-cancel for Solidcore; rules-only event labels."""
    cfg = settings_mod.load_config(Path(tempfile.gettempdir()) / "planwell-no-such-config.json")
    acts = cfg.get("activities")
    if isinstance(acts, dict) and "pilates" in acts:
        acts["pilates"]["late_cancel_hours"] = 12
    else:  # pre-v3 settings module
        cfg.setdefault("late_cancel_hours", {})["pilates"] = 12
    cfg["labeler"] = dict(cfg.get("labeler") or {}, provider="none")
    for k in ("activity_keywords", "activity_labels", "late_cancel_hours"):
        if isinstance(acts, dict) and k in cfg:
            cfg.pop(k)  # derived again on load from `activities`
    return cfg


def ok(result):
    status, payload = result
    if status != 200:
        raise RuntimeError("API returned %s: %s" % (status, payload))
    return payload


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def clean_sample_dir():
    """Remove superseded hand-written files; keep only the generated layout."""
    if not SAMPLE_DIR.exists():
        return 0
    removed = 0
    for p in SAMPLE_DIR.iterdir():
        if p.is_file() and p.suffix == ".json":
            p.unlink()
            removed += 1
    sc = SAMPLE_DIR / "scenarios"
    if sc.exists():
        shutil.rmtree(str(sc))
    return removed


def build(build_date=None, seed=7, days=180, keep_tmp=False, out_dir=None, quiet=False):
    global SAMPLE_DIR
    if out_dir:
        SAMPLE_DIR = Path(out_dir)
    now = demo_now(build_date)
    tmp = Path(tempfile.mkdtemp(prefix="planwell-demo-"))
    cfg_path = tmp / "config.json"
    # Defaults (no personal settings) plus an EXAMPLE 12 h late-cancel policy for Solidcore, so the demo can
    # show the "free cancellation until ..." line. Not a claim about any real studio's policy.
    cfg_path.write_text(json.dumps({"late_cancel_hours": {"pilates": 12, "walking": None}}))
    log = (lambda *a: None) if quiet else print
    log("Demo 'now': %s" % now.isoformat())
    removed = clean_sample_dir()
    log("Removed %d old sample files" % removed)
    has_next48 = hasattr(server.App, "api_next48")
    if not has_next48:
        log("WARNING: server.App.api_next48 not available yet - next48.json NOT written")
    summary = {}
    try:
        for sc in SCENARIOS:
            db_path = tmp / ("mock_%s.db" % sc)
            cal_path = tmp / ("calendar_%s.json" % sc)
            mock_data.generate(days=days, seed=seed, today_preset=sc, db_path=db_path, cal_path=cal_path,
                               now=now, demo_calendar=True)
            engine.clear_cache()
            app = DemoApp(cal_path, config_path=cfg_path, db_path=db_path)
            out = SAMPLE_DIR / "scenarios" / sc
            with frozen_now(now):
                today = ok(app.api_today())
                tomorrow = ok(app.api_tomorrow())
                check_w = ok(app.api_check({"activity": "walking", "start": timeutil.iso(now), "duration_min": 30}))
                check_p = ok(app.api_check({"activity": "pilates", "start": timeutil.iso(now), "duration_min": 50}))
                write_json(out / "today.json", today)
                write_json(out / "tomorrow.json", tomorrow)
                write_json(out / "check_walking.json", check_w)
                write_json(out / "check_pilates.json", check_p)
                if has_next48:
                    write_json(out / "next48.json", ok(app.api_next48()))
                summary[sc] = {
                    "day_status": today["day_status"],
                    "today": [(c["title"], c["start"][11:16], c["verdict"]) for c in today["upcoming"]],
                    "tomorrow": [(c["title"], c["start"][11:16], c["verdict"]) for c in tomorrow["events"]],
                    "check_now": (check_w["verdict"], check_p["verdict"]),
                }
                if has_next48:
                    n48 = json.loads((out / "next48.json").read_text(encoding="utf-8"))
                    summary[sc]["next48"] = n48
                if sc == SHARED_FROM:
                    settings = app.config()
                    sessions = ok(app.api_sessions({"limit": ["100000"]}))
                    insights = ok(app.api_analytics("insights"))
                    backtest = ok(app.api_analytics("backtest"))
                    write_json(SAMPLE_DIR / "settings.json", settings)
                    write_json(SAMPLE_DIR / "sessions.json", sessions)
                    write_json(SAMPLE_DIR / "insights.json", insights)
                    write_json(SAMPLE_DIR / "backtest.json", backtest)
                    missing = []
                    for s in sessions["sessions"]:
                        st, payload = app.api_session(s["workout_id"])
                        if st != 200:
                            missing.append(s["workout_id"])
                            continue
                        write_json(SAMPLE_DIR / ("session_%s.json" % s["workout_id"]), payload)
                    if missing:
                        log("WARNING: %d listed sessions have no detail yet (e.g. %s)" % (len(missing), missing[0]))
                    summary["missing_session_files"] = missing
                    summary["n_sessions_files"] = len(sessions["sessions"]) - len(missing)
                    summary["insights"] = insights
                    summary["backtest"] = backtest
    finally:
        engine.clear_cache()
        if keep_tmp:
            log("Temp files kept in %s" % tmp)
        else:
            shutil.rmtree(str(tmp), ignore_errors=True)
    return now, summary


def main(argv=None):
    ap = argparse.ArgumentParser(description="Build web/sample/ from real engine output on mock data")
    ap.add_argument("--date", help="build date (default today); demo 'now' = the Wednesday before it, 15:30")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--keep-tmp", action="store_true")
    args = ap.parse_args(argv)
    bd = date.fromisoformat(args.date) if args.date else None
    now, s = build(bd, seed=args.seed, keep_tmp=args.keep_tmp)
    for sc in SCENARIOS:
        r = s[sc]
        print("\n[%s] day: %s   check-now walk/Solidcore: %s/%s" % (sc, r["day_status"], *r["check_now"]))
        for t, hm, v in r["today"]:
            print("   upcoming  %s  %-28s %s" % (hm, t, v))
        for t, hm, v in r["tomorrow"]:
            print("   tomorrow  %s  %-28s %s" % (hm, t, v))
        for d in (r.get("next48") or {}).get("days", []):
            ld = d.get("load") or {}
            print("   next48 %-9s load %s (expected %s, typical %s) - %s"
                  % (d.get("label"), ld.get("level"), ld.get("expected"), ld.get("typical"), d.get("headline")))
            for it in d.get("items", []):
                c = it.get("check") or {}
                al = it.get("ai_label") or {}
                print("      %s  %-28s %-9s %-7s %s" % (
                    (it.get("start") or "")[11:16], it.get("title"), it.get("label") or "-",
                    c.get("verdict") or ("untracked" if not it.get("tracked") else "-"),
                    ("exertion %s/%s" % (al.get("exertion"), al.get("source"))) if al else ""))
            for sg in d.get("suggestions", []):
                print("      suggestion [%s] %s" % (sg.get("type"), sg.get("text")))
    b = s["backtest"]
    print("\nBacktest (%d sessions judged, %d skipped): %s" % (b["n_sessions"], b["n_skipped"], b["hard_definition"]))
    for k in ("planwell", "baseline"):
        m = b[k]
        print("   %-26s tp=%d fp=%d fn=%d tn=%d  precision=%.2f recall=%.2f flag_rate=%.2f"
              % (m.get("name", "PlanWell (yellow or red)"), m["tp"], m["fp"], m["fn"], m["tn"],
                 m["precision"], m["recall"], m["flag_rate"]))
    for v in b["by_verdict"]:
        print("   %-6s n=%-3d avg %s min/30, %s hard" % (v["verdict"], v["n"], v["avg_minutes"], v["pct_hard"]))
    print("\nInsights:")
    for i in s["insights"]["insights"]:
        print("   [%s/%s] %s" % (i["id"], i["confidence"], i["finding"]))
    print("\nWrote %d session files + scenario files to %s" % (s["n_sessions_files"], SAMPLE_DIR))
    return 0


if __name__ == "__main__":
    sys.exit(main())
