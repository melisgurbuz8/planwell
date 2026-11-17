"""Nightly job (launchd, 03:30): Oura sync of the last 3 days + calendar history refresh.

    python3 -m planwell.nightly            # real: data/planwell.db
    python3 -m planwell.nightly --dry-run  # log what would run, touch nothing

Calendar history = the last 14 days and next 3 days of events from every configured calendar,
stored in calendar_events (used for daily-kind sessions such as classes).
Never raises; every step is logged to data/nightly.log (and stdout).
"""
import argparse
import sys
import time
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = REPO_ROOT / "data" / "nightly.log"
OURA_DAYS = 3
CAL_BACK_DAYS = 14
CAL_AHEAD_DAYS = 3


def log(msg, path=None):
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        p = Path(path) if path else LOG_PATH
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(str(p), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def run(db_path=None, config=None, sync_fn=None, history_fn=None, log_path=None, dry_run=False):
    """Run both steps; returns {"oura": {...}|None, "calendar": n|None, "errors": [...]}. Never raises."""
    out = {"oura": None, "calendar": None, "errors": []}
    say = lambda m: log(m, log_path)  # noqa: E731
    try:
        if config is None:
            from planwell.settings import load_config
            config = load_config()
    except Exception as e:
        out["errors"].append("config: %s" % e)
        say("Could not load config: %s" % e)
        return out
    if dry_run:
        say("Dry run: would sync Oura (last %d days) and calendar (-%d/+%d days)."
            % (OURA_DAYS, CAL_BACK_DAYS, CAL_AHEAD_DAYS))
        return out

    # 1) Oura
    try:
        if sync_fn is None:
            from planwell import sync as _sync
            sync_fn = _sync.sync
        kw = {"days": OURA_DAYS}
        if db_path:
            kw["db_path"] = db_path
        res = sync_fn(**kw)
        out["oura"] = res
        if isinstance(res, dict) and not res.get("ok", True):
            out["errors"].append("oura: %s" % res.get("error"))
            say("Oura sync had a problem: %s" % res.get("error"))
        else:
            rows = (res or {}).get("rows") if isinstance(res, dict) else None
            say("Oura sync ok%s." % (" (%s)" % rows if rows else ""))
    except Exception as e:
        out["errors"].append("oura: %s" % e)
        say("Oura sync failed: %s" % e)

    # 2) Calendar history
    try:
        if history_fn is None:
            from planwell import gcal
            if not gcal.is_connected():
                raise RuntimeError("Google Calendar isn't connected (run python3 -m planwell.gcal)")
            history_fn = gcal.pull_history
        n = history_fn(CAL_BACK_DAYS, CAL_AHEAD_DAYS, config, db_path=db_path)
        out["calendar"] = n
        say("Calendar history ok: %s events stored." % n)
    except Exception as e:
        out["errors"].append("calendar: %s" % e)
        say("Calendar history failed: %s" % e)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="PlanWell nightly sync")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    try:
        run(dry_run=args.dry_run)
    except Exception:  # belt and braces: never fail the launchd job
        log("Nightly job crashed:\n" + traceback.format_exc())
    return 0


if __name__ == "__main__":
    sys.exit(main())
