"""Pre-activity watcher: about `precheck_minutes` before each walking/pilates event, sync recent
Oura data, run the check and notify once per event.

    python3 -m planwell.watcher --once            # one pass (what launchd runs every 10 min)
    python3 -m planwell.watcher --loop            # keep running, one pass every 10 min
    python3 -m planwell.watcher --once --mock     # mock DB + mock calendar, no Oura sync
    python3 -m planwell.watcher --once --dry-run  # print notifications instead of sending

An event is due when it starts within (precheck_minutes + 10) minutes and has not started yet
(so a pass that was skipped while the laptop slept still produces a slightly-late notice).
Morning digest: at config morning.time (default 07:00; up to 4 h late if the Mac was asleep) one
"next 48 hours" notification (deduped as morning@<date>), built by engine.build_next48.
Night-before preview: once the earliest preview_due for tomorrow is reached (preview.time, or
earlier when a late-cancel window needs it), ONE combined "tomorrow" notification is sent
(deduped as preview@<date>). The 1-hour prechecks still run as usual.
Notifications go to macOS (osascript) and to the phone via ntfy when a topic is set up
(`python3 -m planwell.notify_setup`; the topic lives in secrets/ntfy.json, never in config.json).

Phone scheduling (the Mac sleeps a lot): every pass also computes everything due in the next
36 h - the morning digest, the night-before preview and the 1-hour prechecks - and publishes each
to ntfy with an `At:` delivery time, so the phone gets it on time even if the Mac is asleep then.
Deduped in `notified` as sched:<kind>@<id>@<start> with the verdict that was scheduled. If a later
pass (fresher data) computes a different verdict, one short "Update: ..." is sent immediately.
Anything due in under a minute is sent right away. Scheduled texts say which data they're based on.
Mac banners: when the Mac is awake at the due time it still shows its local banner (it may carry a
fresher verdict and never leaves the Mac), but the phone is not sent the same item twice.
"""
import argparse
import sys
import time
from datetime import timedelta

from planwell import db, engine, gcal
from planwell import notify as nt
from planwell.classify import classify
from planwell.settings import load_config
from planwell.timeutil import fmt_clock, fmt_hm, get_tz, iso, now_in, parse_iso

WINDOW_SLACK_MIN = 10
LOOP_SECONDS = 600
VERDICT_WORD = {"green": "Looks good", "yellow": "Take it easier", "red": "Gentler plan suggested"}
NTFY_TAGS = {"green": "green_circle", "yellow": "yellow_circle", "red": "red_circle"}


def log(msg):
    print("[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)


# ---------------------------------------------------------------- channels
def _osa_escape(s):
    return (s or "").replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def notify_macos(title, body):
    import subprocess
    script = "on run argv\ndisplay notification (item 1 of argv) with title (item 2 of argv)\nend run"
    try:
        subprocess.run(["osascript", "-e", script, _osa_escape(body), _osa_escape(title)], check=True, timeout=15,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        return True
    except Exception as e:
        log("macOS notification failed: %s" % e)
        return False


_header_safe = nt.header_safe


def notify_ntfy(topic, title, body, verdict, server="https://ntfy.sh", at=None):
    """Publish to ntfy (Click -> PlanWell on the Mac; Authorization from secrets/env if set)."""
    return nt.publish(topic, title, body, verdict, server, at=at, log=log)


# ---------------------------------------------------------------- message
def top_option(chk):
    opts = chk.get("options") or []
    if chk.get("verdict") in ("yellow", "red"):
        for o in opts:
            if o.get("type") != "go":
                return o
    return opts[0] if opts else None


def compose(chk, tz, stale_note=None):
    start = parse_iso(chk["start"], tz).astimezone(tz)
    title = "PlanWell: %s \u2014 %s at %s" % (VERDICT_WORD.get(chk["verdict"], ""), chk["title"], fmt_clock(start))
    head = chk.get("headline", "")
    parts = [head + ("." if head and head[-1] not in ".!?" else "")]
    if chk.get("reasons"):
        parts.append(chk["reasons"][0] + ".")
    opt = top_option(chk)
    if opt:
        parts.append("Option: " + opt["label"] + ".")
    if stale_note:
        parts.append(stale_note)
    body = " ".join(p for p in parts if p).replace("..", ".")
    return title, body


def _dedupe_key(ev):
    # include the start so a rescheduled event gets a fresh check
    return "%s@%s" % (ev.get("id"), ev.get("start"))


PREVIEW_WORD = {"green": "looks good", "yellow": "maybe take it easier", "red": "a gentler plan may suit"}


def compose_preview(tm, tz, now, stale_note=None):
    """One combined night-before message for /api/tomorrow-shaped `tm`."""
    title = "PlanWell: tomorrow's early look"
    lines = [tm.get("headline", "").rstrip(".") + "."]
    for c in tm.get("events") or []:
        s = parse_iso(c["start"], tz).astimezone(tz)
        line = "%s %s: %s" % (c.get("label") or c["activity"], fmt_clock(s), PREVIEW_WORD.get(c["verdict"], ""))
        opt = top_option(c)
        if c["verdict"] != "green" and opt and opt.get("type") != "go":
            line += " (option: %s)" % opt["label"]
        line += "."
        if c.get("cancel_by") and c["verdict"] != "green":
            cb = parse_iso(c["cancel_by"], tz).astimezone(tz)
            if cb > now:
                when = fmt_clock(cb) if cb.date() == now.astimezone(tz).date() else \
                    cb.strftime("%a ") + fmt_clock(cb)
                line += " If you'd rather cancel, deciding by %s avoids a late fee." % when
            else:
                line += " The late-cancel window has passed \u2014 that's okay; a gentler version still works."
        lines.append(line)
    if tm.get("note"):
        lines.append(tm["note"])
    if stale_note:
        lines.append(stale_note)
    return title, " ".join(lines)


def _deliver(title, body, verdict, payload, config, dry_run, send, summary=None, ntfy=True):
    """Live delivery: macOS banner + (unless ntfy=False) the phone at the configured detail level.

    summary = (title, body) for the "summary" level; without one, summary falls back to minimal.
    """
    if dry_run:
        log("DRY RUN %s | %s" % (title, body))
        return True
    if send is not None:
        return bool(send(title, body, payload))
    sent = False
    nc = config.get("notify") or {}
    if nc.get("macos", True) and sys.platform == "darwin":
        sent = notify_macos(title, body) or sent
    topic = nt.topic_for(config) if ntfy else None
    if topic:
        server = nt.server_for(config)
        if not server.startswith("https://"):
            log("ntfy server must use https; not sending.")
        else:
            # ntfy topics are readable by anyone who knows the name: by default send only the
            # activity, time and verdict word (summary); "minimal" sends a bare nudge.
            st, sb = summary if summary else (None, None)
            t, b, v = nt.render(nt.detail_for(config), title, body, st, sb, verdict)
            sent = notify_ntfy(topic, t, b, v, server) or sent
    if not sent and ntfy:
        log("No notification channel delivered (enable macOS or run python3 -m planwell.notify_setup). %s | %s"
            % (title, body))
    return sent


def _mark(db_path, key, now, verdict):
    conn = db.connect(db_path)
    try:
        db.mark_notified(conn, key, iso(now), verdict)
    finally:
        conn.close()


MORNING_LATE_HOURS = 4  # still send the digest if the laptop woke up a little late
NOTE_BY_VERDICT = {"yellow": " (take it easier)", "red": " (go gently)"}


def morning_due(config, now):
    from planwell.settings import parse_hhmm
    tz = get_tz(config)
    hm = parse_hhmm((config.get("morning") or {}).get("time", "07:00")) or (7, 0)
    return now.astimezone(tz).replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)


def _clock_lc(dt):
    return fmt_clock(dt).replace(" AM", " am").replace(" PM", " pm")


def compose_morning(n48, tz, stale_note=None):
    """'Today: heavier than usual \u2014 Class 9:30 am, Padel 7:00 pm (go gently). Tomorrow: usual.'"""
    parts = []
    for d in n48.get("days") or []:
        lvl = engine.LOAD_WORDS.get(d["load"]["level"], d["load"]["level"])
        tracked = [it for it in d["items"] if it["tracked"]]
        if not tracked:
            parts.append("%s: nothing tracked planned." % d["label"])
            continue
        if d["label"] == "Today":
            bits = []
            for it in tracked:
                note = NOTE_BY_VERDICT.get((it.get("check") or {}).get("verdict"), "")
                bits.append("%s %s%s" % (it["label"], _clock_lc(parse_iso(it["start"], tz).astimezone(tz)), note))
            parts.append("%s: %s \u2014 %s." % (d["label"], lvl, ", ".join(bits)))
        else:
            parts.append("%s: %s." % (d["label"], lvl))
    today = (n48.get("days") or [{}])[0]
    if today.get("suggestions"):
        parts.append(today["suggestions"][0]["text"])
    if stale_note:
        parts.append(stale_note)
    return "PlanWell: your next 48 hours", " ".join(parts)


def preview_state(events, config, now):
    """(tomorrow_date_str, earliest preview_due or None) for tomorrow's walk/Solidcore events."""
    tz = get_tz(config)
    tomorrow = now.astimezone(tz).date() + timedelta(days=1)
    dues = []
    for ev in events or []:
        act = classify(ev, config)
        s = parse_iso(ev.get("start"), tz)
        if act and engine.kind_of(act, config) == "exercise" and s and s.astimezone(tz).date() == tomorrow:
            dues.append(engine.preview_due_for(s, act, config, now))
    return tomorrow.isoformat(), (min(dues) if dues else None)


# ---------------------------------------------------------------- summary texts (no body numbers)
DETAILS_TAIL = " Open PlanWell for details."


def summary_precheck(chk, tz):
    s = parse_iso(chk["start"], tz).astimezone(tz)
    return "PlanWell", nt.summary_line(chk.get("label") or chk["activity"], _clock_lc(s), chk["verdict"]) + "." + DETAILS_TAIL


def summary_morning(n48, tz):
    bits = []
    for d in (n48.get("days") or [])[:1]:
        for it in d["items"]:
            if it["tracked"] and it.get("check"):
                bits.append(nt.summary_line(it["label"], _clock_lc(parse_iso(it["start"], tz).astimezone(tz)),
                                            it["check"]["verdict"]))
    body = ("Today: " + "; ".join(bits) + ".") if bits else "Today: nothing tracked planned."
    return "PlanWell: today", body + DETAILS_TAIL


def summary_preview(tm, tz):
    bits = [nt.summary_line(c.get("label") or c["activity"], _clock_lc(parse_iso(c["start"], tz).astimezone(tz)),
                            c["verdict"]) for c in tm.get("events") or []]
    return "PlanWell: tomorrow", "Tomorrow: " + "; ".join(bits) + "." + DETAILS_TAIL


def worst_verdict(verdicts):
    vs = [v for v in verdicts if v in engine.VERDICT_RANK]
    return max(vs, key=lambda v: engine.VERDICT_RANK[v]) if vs else "green"


def as_of_text(db_path, due, tz):
    """'as of last night (10:40 pm)' - when the newest Oura data was recorded, relative to `due`."""
    st = engine.get_store(db_path)
    ts = st.latest_hr or st.last_sync
    if ts is None:
        return "with no recent Oura data"
    d, due = ts.astimezone(tz), due.astimezone(tz)
    clock = _clock_lc(d)
    if d.date() == due.date():
        return "as of %s today" % clock
    if (due.date() - d.date()).days == 1:
        return ("as of last night (%s)" % clock) if d.hour >= 17 else ("as of yesterday %s" % clock)
    return "as of %s %s" % (d.strftime("%a"), clock)


# ---------------------------------------------------------------- phone scheduling
SCHEDULE_HOURS = 36
SEND_NOW_SECONDS = 60
SCHED_SYNC_MINUTES = 50  # schedule-only passes refresh Oura at most about hourly


def sched_key(kind, item_id, start):
    return "sched:%s@%s@%s" % (kind, item_id, start)


def live_to_sched(kind, live_key):
    """Live dedupe key -> matching phone-schedule key."""
    if kind == "precheck":
        return "sched:precheck@" + live_key
    if kind == "morning":
        return sched_key("morning", "digest", live_key.split("@", 1)[1])
    if kind == "preview":
        return sched_key("preview", "tomorrow", live_key.split("@", 1)[1])
    return "sched:%s@%s" % (kind, live_key)


def _stored_verdict(db_path, key):
    conn = db.connect(db_path)
    try:
        row = conn.execute("SELECT verdict FROM notified WHERE event_id=?", (key,)).fetchone()
        return row["verdict"] if row else None
    finally:
        conn.close()


def plan_schedule(db_path, config, events, now):
    """Everything due in (now, now + 36 h]: [{kind, key, due, verdict, full, summary, label_line}]."""
    tz = get_tz(config)
    now = now.astimezone(tz)
    horizon = now + timedelta(hours=SCHEDULE_HOURS)
    precheck = int(config.get("precheck_minutes", 60))
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    items = []
    # 1-hour prechecks for tracked exercise
    for ev in events or []:
        act = classify(ev, config)
        s, e = parse_iso(ev.get("start"), tz), parse_iso(ev.get("end"), tz)
        if not act or not s or engine.kind_of(act, config) != "exercise":
            continue
        due = s - timedelta(minutes=precheck)
        if not (now <= due <= horizon):
            continue
        dur = int(round((e - s).total_seconds() / 60)) if e and e > s else 30
        chk = engine.check(db_path, config, act, s, dur, now=due, event=ev)
        items.append({"kind": "precheck", "key": sched_key("precheck", ev.get("id"), ev.get("start")),
                      "due": due, "verdict": chk["verdict"], "full": compose(chk, tz),
                      "summary": summary_precheck(chk, tz),
                      "what": "%s %s" % (chk.get("label") or act, _clock_lc(s.astimezone(tz)))})
    for offset in (0, 1):
        dstart = day0 + timedelta(days=offset)
        # morning digest at morning.time
        if (config.get("morning") or {}).get("enabled", True):
            T = morning_due(config, dstart)
            if now <= T <= horizon:
                n48 = engine.build_next48(db_path, config, events, now=T)
                if any(it["tracked"] for d in n48["days"] for it in d["items"]):
                    v = worst_verdict(it["check"]["verdict"] for it in n48["days"][0]["items"] if it.get("check"))
                    items.append({"kind": "morning", "key": sched_key("morning", "digest", T.date().isoformat()),
                                  "due": T, "verdict": v, "full": compose_morning(n48, tz),
                                  "summary": summary_morning(n48, tz), "what": "this morning's digest"})
        # night-before preview (preview.time, or earlier when a late-cancel window needs it)
        if (config.get("preview") or {}).get("enabled", True):
            ref = max(now, dstart)
            pdate, pdue = preview_state(events, config, ref)
            if pdue is None:
                continue
            pdue = max(pdue, now)
            if pdue > horizon:
                continue
            tm = engine.build_tomorrow(db_path, config, events, now=max(pdue, dstart))
            if tm["events"] and tm["date"] == pdate:
                items.append({"kind": "preview", "key": sched_key("preview", "tomorrow", pdate),
                              "due": pdue, "verdict": tm["day_status"], "full": compose_preview(tm, tz, pdue),
                              "summary": summary_preview(tm, tz), "what": "tomorrow's early look"})
    items.sort(key=lambda it: it["due"])
    return items


def update_texts(item, new_verdict):
    """(full, summary, minimal) texts for an immediate 'Update: ...' message."""
    word = nt.SUMMARY_WORD.get(new_verdict, new_verdict)
    full = ("PlanWell update", "Update: %s — now %s (fresher data). This replaces the earlier note."
            % (item["what"], word))
    return full, full


def schedule_pass(db_path, config, events, now, dry_run=False, publish=None, topic=None):
    """Publish upcoming notifications to ntfy with `At:`; send 'Update' when a verdict changed."""
    topic = topic or nt.topic_for(config)
    if not topic:
        return []
    tz = get_tz(config)
    server = nt.server_for(config)
    if not server.startswith("https://"):
        log("ntfy server must use https; not scheduling.")
        return []
    detail = nt.detail_for(config)
    pub = publish or (lambda tp, t, b, v, at=None: notify_ntfy(tp, t, b, v, server, at=at))
    out = []
    for it in plan_schedule(db_path, config, events, now):
        prev = _stored_verdict(db_path, it["key"])
        if prev == it["verdict"]:
            continue  # already scheduled with this verdict - no double send
        if prev is not None:
            full, summ = update_texts(it, it["verdict"])
            t, b, v = nt.render(detail, full[0], full[1], summ[0], summ[1], it["verdict"])
            if detail == "minimal":
                b = "Update: a PlanWell check changed with fresher data. Open PlanWell to see it."
            at, kind = None, "update"
        else:
            note = " (Based on Oura data %s.)" % as_of_text(db_path, it["due"], tz)
            t, b, v = nt.render(detail, it["full"][0], it["full"][1] + note,
                                it["summary"][0], it["summary"][1] + note, it["verdict"])
            if detail == "minimal":
                b = "You have a PlanWell check coming up. Open PlanWell on your Mac to see it." + note
            delay = (it["due"] - now).total_seconds()
            at = None if delay < SEND_NOW_SECONDS else int(it["due"].timestamp())
            kind = "scheduled" if at else "sent_now"
        if dry_run:
            log("DRY RUN ntfy %s%s | %s | %s" % (kind, (" at %s" % iso(it["due"])) if at else "", t, b))
            ok = True
        else:
            ok = pub(topic, t, b, v, at=at)
            if ok:
                _mark(db_path, it["key"], now, it["verdict"])
        log("Phone %s: %s -> %s%s" % (kind, it["key"], it["verdict"], "" if ok else " (FAILED)"))
        out.append({"kind": kind, "event_id": it["key"], "verdict": it["verdict"], "title": t, "body": b,
                    "at": at, "sent": ok, "item_kind": it["kind"]})
    return out


# ---------------------------------------------------------------- one pass
def run_once(mock=False, dry_run=False, now=None, config=None, db_path=None, sync_fn=None,
             events=None, send=None, publish=None):
    """One watcher pass: live 1-hour prechecks, preview and morning digest, then phone scheduling.

    `send` replaces every live channel (tests); `publish(topic, title, body, verdict, at=None)`
    replaces ntfy for the phone schedule. With `send` but no `publish`, phone scheduling is skipped
    (so injected test runs can never reach a real phone).
    Returns a list of {kind, event_id, verdict, title, body, sent}.
    """
    config = config or load_config()
    tz = get_tz(config)
    now = now_in(config, now)
    db_path = str(db_path or db.default_path(mock))
    precheck = int(config.get("precheck_minutes", 60))
    horizon = now + timedelta(minutes=precheck + WINDOW_SLACK_MIN)
    day0 = now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    # never schedule mock data onto the real phone; injected test runs need an explicit `publish`
    scheduling = (send is None or publish is not None) and (not mock or publish is not None or dry_run)
    topic = nt.topic_for(config) if scheduling else None

    if events is None:
        try:
            events = gcal.get_events(now, day0 + timedelta(days=4), config, mock=mock)
        except gcal.CalendarNotConnected as e:
            log("Calendar not connected: %s" % e)
            return []
        except Exception as e:
            log("Calendar read failed: %s" % e)
            return []
    due = []
    for ev in events:
        act = classify(ev, config)
        s = parse_iso(ev.get("start"), tz)
        # 1-hour prechecks are for exercise; daily kinds (e.g. classes) appear in the morning digest
        if act and engine.kind_of(act, config) == "exercise" and s and now < s <= horizon:
            due.append((ev, act))

    preview_key = None
    pv = config.get("preview") or {}
    if pv.get("enabled", True):
        pdate, pdue = preview_state(events, config, now)
        if pdue is not None and now >= pdue:
            preview_key = "preview@%s" % pdate

    morning_key = None
    mc = config.get("morning") or {}
    if mc.get("enabled", True):
        mdue = morning_due(config, now)
        if mdue <= now < mdue + timedelta(hours=MORNING_LATE_HOURS):
            morning_key = "morning@%s" % now.astimezone(tz).date().isoformat()

    conn = db.connect(db_path)
    try:
        due = [(ev, a) for ev, a in due if not db.was_notified(conn, _dedupe_key(ev))]
        if preview_key and db.was_notified(conn, preview_key):
            preview_key = None
        if morning_key and db.was_notified(conn, morning_key):
            morning_key = None
    finally:
        conn.close()
    live = bool(due or preview_key or morning_key)
    if not live and not topic:
        log("Nothing due (no tracked exercise within %d min, no preview or morning digest pending)."
            % (precheck + WINDOW_SLACK_MIN))
        return []

    stale_note = None
    last = engine.get_store(db_path).last_sync
    want_sync = live or last is None or (now - last).total_seconds() > SCHED_SYNC_MINUTES * 60
    if not mock and want_sync:
        try:
            if sync_fn is None:
                from planwell import sync as _sync
                sync_fn = _sync.sync
            res = sync_fn(recent=True)
            if isinstance(res, dict) and not res.get("ok", True):
                raise RuntimeError(res.get("error") or "sync reported a problem")
            log("Synced recent Oura data.")
        except Exception as e:
            log("Sync failed, using existing data: %s" % e)
            stale_note = "(Couldn't refresh Oura just now"
    if stale_note is not None:
        age = engine._freshness(engine.get_store(db_path), now).get("minutes_old")
        stale_note += ("; data is %s old.)" % fmt_hm(age)) if age is not None else ".)"

    out = []

    def live_deliver(kind, live_key, title, body, verdict, payload, summary, what):
        """Mac banner always; phone only if not already scheduled with this verdict (else 'Update')."""
        skey = live_to_sched(kind, live_key)
        prev = _stored_verdict(db_path, skey) if topic else None
        if prev is None:
            ok = _deliver(title, body, verdict, payload, config, dry_run, send, summary=summary)
        else:
            ok = _deliver(title, body, verdict, payload, config, dry_run, send, summary=summary, ntfy=False)
            if prev != verdict:
                full, summ = update_texts({"what": what}, verdict)
                t, b, v = nt.render(nt.detail_for(config), full[0], full[1], summ[0], summ[1], verdict)
                if dry_run:
                    log("DRY RUN ntfy update | %s | %s" % (t, b))
                elif (publish or (lambda tp, t_, b_, v_, at=None: notify_ntfy(tp, t_, b_, v_, nt.server_for(config))))(
                        topic, t, b, v, at=None):
                    ok = True
            elif send is None:
                ok = True  # the phone already has it; the Mac banner is a bonus
        if ok and not dry_run:
            _mark(db_path, live_key, now, verdict)
            if topic:
                _mark(db_path, skey, now, verdict)
        return ok

    if morning_key:
        n48 = engine.build_next48(db_path, config, events, now=now)
        if any(it["tracked"] for d in n48["days"] for it in d["items"]):
            title, body = compose_morning(n48, tz, stale_note)
            worst = worst_verdict(it["check"]["verdict"] for it in n48["days"][0]["items"] if it.get("check"))
            sent = live_deliver("morning", morning_key, title, body, worst, n48, summary_morning(n48, tz),
                                "this morning's digest")
            log("Morning digest -> %s" % worst)
            out.append({"kind": "morning", "event_id": morning_key, "verdict": worst,
                        "title": title, "body": body, "sent": sent})
    for ev, act in due:
        s, e = parse_iso(ev["start"], tz), parse_iso(ev.get("end"), tz)
        dur = int(round((e - s).total_seconds() / 60)) if e and e > s else 30
        chk = engine.check(db_path, config, act, s, dur, now=now, event=ev)
        title, body = compose(chk, tz, stale_note)
        sent = live_deliver("precheck", _dedupe_key(ev), title, body, chk["verdict"], chk, summary_precheck(chk, tz),
                            "%s %s" % (chk.get("label") or act, _clock_lc(s.astimezone(tz))))
        log("%s %s -> %s" % (ev.get("title"), ev.get("start"), chk["verdict"]))
        out.append({"kind": "precheck", "event_id": ev.get("id"), "verdict": chk["verdict"],
                    "title": title, "body": body, "sent": sent})

    if preview_key:
        tm = engine.build_tomorrow(db_path, config, events, now=now)
        if tm["events"]:
            title, body = compose_preview(tm, tz, now, stale_note)
            sent = live_deliver("preview", preview_key, title, body, tm["day_status"], tm, summary_preview(tm, tz),
                                "tomorrow's early look")
            log("Tomorrow preview (%s) -> %s" % (tm["date"], tm["day_status"]))
            out.append({"kind": "preview", "event_id": preview_key, "verdict": tm["day_status"],
                        "title": title, "body": body, "sent": sent})

    if topic:
        try:
            out += schedule_pass(db_path, config, events, now, dry_run=dry_run, publish=publish, topic=topic)
        except Exception as e:  # scheduling must never break live checks
            log("Phone scheduling failed: %r" % e)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="PlanWell pre-activity watcher")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--once", action="store_true", help="run one pass (default)")
    g.add_argument("--loop", action="store_true", help="run a pass every 10 minutes")
    ap.add_argument("--mock", action="store_true", help="use data/mock.db + data/mock_calendar.json")
    ap.add_argument("--dry-run", action="store_true", help="print instead of sending notifications")
    args = ap.parse_args(argv)
    while True:
        try:
            run_once(mock=args.mock, dry_run=args.dry_run)
        except Exception as e:  # never crash the launchd job
            log("Watcher pass failed: %r" % e)
        if not args.loop:
            return 0
        time.sleep(LOOP_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
