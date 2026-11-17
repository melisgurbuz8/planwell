"""Phone notifications: ntfy scheduling, dedupe, updates, detail levels, token, topic secrets."""
import copy
import json
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fixture  # noqa: E402
from planwell import engine, notify as nt, server, settings, watcher  # noqa: E402

TZ = fixture.TZ
TOPIC = "pw_test_topic_0123456789abcdef"
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def at(h, m=0, s=0, day=None):
    d = day or fixture.TODAY
    return datetime(d.year, d.month, d.day, h, m, s, tzinfo=TZ)


def ev(eid, title, start, minutes):
    return {"id": eid, "title": title, "start": start.isoformat(),
            "end": (start + timedelta(minutes=minutes)).isoformat(), "location": ""}


D1 = fixture.TODAY + timedelta(days=1)
D2 = fixture.TODAY + timedelta(days=2)
EVENTS = [ev("w1", "Evening walk", at(17), 30),
          ev("s1", "Solidcore", at(9, 0, day=D1), 50),
          ev("far", "Morning walk", at(9, 0, day=D2), 30),      # due outside the 36 h window
          ev("x1", "Dentist", at(11, 0, day=D1), 60)]


def cfg_with(detail="summary", **extra):
    c = copy.deepcopy(settings.DEFAULT_CONFIG)
    c["notify"] = {"macos": False, "ntfy_detail": detail, "ntfy_server": "https://ntfy.sh",
                   "ntfy_topic": TOPIC}  # back-compat: a real topic in the dict is used (tests only)
    c.update(extra)
    return c


class Recorder(object):
    def __init__(self, ok=True):
        self.calls, self.ok = [], ok

    def __call__(self, topic, title, body, verdict, at=None):
        self.calls.append({"topic": topic, "title": title, "body": body, "verdict": verdict, "at": at})
        return self.ok


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="planwell-notify-")
        cls.src_db = fixture.build(os.path.join(cls.tmp, "src.db"), today_low=True, latest_hr_at=at(14, 50))
        engine.clear_cache()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def fresh_db(self, name):
        p = os.path.join(self.tmp, name)
        shutil.copy(self.src_db, p)
        return p


class TestPlanSchedule(Base):
    def test_items_and_due_times(self):
        items = watcher.plan_schedule(self.src_db, cfg_with(), EVENTS, at(15))
        by_kind = {}
        for it in items:
            by_kind.setdefault(it["kind"], []).append(it)
        pre = {it["key"]: it["due"] for it in by_kind["precheck"]}
        self.assertEqual(pre, {"sched:precheck@w1@%s" % at(17).isoformat(): at(16),
                               "sched:precheck@s1@%s" % at(9, 0, day=D1).isoformat(): at(8, 0, day=D1)})
        self.assertEqual([(it["key"], it["due"]) for it in by_kind["morning"]],
                         [("sched:morning@digest@%s" % D1.isoformat(), at(7, 0, day=D1))])  # today's 7:00 passed
        # tonight's preview for tomorrow (Solidcore) and tomorrow night's for the day after ("far" walk)
        self.assertEqual([(it["key"], it["due"]) for it in by_kind["preview"]],
                         [("sched:preview@tomorrow@%s" % D1.isoformat(), at(20)),
                          ("sched:preview@tomorrow@%s" % D2.isoformat(), at(20, day=D1))])
        self.assertNotIn("far", " ".join(it["key"] for it in items))
        self.assertEqual([it["due"] for it in items], sorted(it["due"] for it in items))

    def test_late_cancel_moves_preview_earlier(self):
        c = cfg_with()
        c["activities"]["pilates"]["late_cancel_hours"] = 12
        c["late_cancel_hours"]["pilates"] = 12
        items = watcher.plan_schedule(self.src_db, c, EVENTS, at(15))
        prev = [it for it in items if it["kind"] == "preview"][0]
        self.assertEqual(prev["due"], at(20))  # min(20:00, 09:00 - 12 h - 1 h = 20:00)
        c["activities"]["pilates"]["late_cancel_hours"] = 14
        c["late_cancel_hours"]["pilates"] = 14
        prev = [it for it in watcher.plan_schedule(self.src_db, c, EVENTS, at(15)) if it["kind"] == "preview"][0]
        self.assertEqual(prev["due"], at(18))

    def test_disabled_kinds(self):
        c = cfg_with(morning={"enabled": False, "time": "07:00"}, preview={"enabled": False, "time": "20:00"})
        kinds = {it["kind"] for it in watcher.plan_schedule(self.src_db, c, EVENTS, at(15))}
        self.assertEqual(kinds, {"precheck"})


class TestSchedulePass(Base):
    def test_schedules_with_at_and_as_of_and_dedupes(self):
        db = self.fresh_db("s1.db")
        rec = Recorder()
        out = watcher.schedule_pass(db, cfg_with(), EVENTS, at(15), publish=rec)
        self.assertEqual(len(out), 5)  # 2 prechecks, morning, 2 previews
        self.assertTrue(all(o["kind"] == "scheduled" for o in out))
        ats = sorted(c["at"] for c in rec.calls)
        self.assertEqual(ats[0], int(at(16).timestamp()))
        for c in rec.calls:
            self.assertEqual(c["topic"], TOPIC)
            self.assertIn("Based on Oura data as of", c["body"])
            self.assertNotIn("HRV", c["body"])
            self.assertNotIn("bpm", c["body"])
            self.assertNotIn("%", c["body"])
            self.assertIn("Open PlanWell for details.", c["body"])
        walk = [c for c in rec.calls if c["at"] == int(at(16).timestamp())][0]
        self.assertTrue(walk["body"].startswith("Walk 5:00 pm — "), walk["body"])
        self.assertIn(walk["verdict"], ("green", "yellow", "red"))
        # verdict stored for each scheduled item
        conn = sqlite3.connect(db)
        rows = dict(conn.execute("SELECT event_id, verdict FROM notified WHERE event_id LIKE 'sched:%'").fetchall())
        conn.close()
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows["sched:precheck@w1@%s" % at(17).isoformat()], walk["verdict"])
        # next pass: same data, same verdicts -> nothing re-sent
        rec2 = Recorder()
        self.assertEqual(watcher.schedule_pass(db, cfg_with(), EVENTS, at(15, 10), publish=rec2), [])
        self.assertEqual(rec2.calls, [])

    def test_verdict_change_sends_one_update(self):
        db = self.fresh_db("s2.db")
        watcher.schedule_pass(db, cfg_with(), EVENTS, at(15), publish=Recorder())
        key = "sched:precheck@w1@%s" % at(17).isoformat()
        conn = sqlite3.connect(db)
        real = conn.execute("SELECT verdict FROM notified WHERE event_id=?", (key,)).fetchone()[0]
        old = "green" if real != "green" else "red"
        conn.execute("UPDATE notified SET verdict=? WHERE event_id=?", (old, key))  # pretend data changed
        conn.commit()
        conn.close()
        rec = Recorder()
        out = watcher.schedule_pass(db, cfg_with(), EVENTS, at(15, 10), publish=rec)
        self.assertEqual([o["kind"] for o in out], ["update"])
        self.assertIsNone(rec.calls[0]["at"])  # immediate
        self.assertTrue(rec.calls[0]["body"].startswith("Update: Walk 5:00 pm — now "))
        self.assertIn(nt.SUMMARY_WORD[real], rec.calls[0]["body"])
        # and only once
        self.assertEqual(watcher.schedule_pass(db, cfg_with(), EVENTS, at(15, 20), publish=Recorder()), [])

    def test_due_within_a_minute_sends_now(self):
        db = self.fresh_db("s3.db")
        rec = Recorder()
        out = watcher.schedule_pass(db, cfg_with(), [EVENTS[0]], at(15, 59, 30), publish=rec)
        pre = [o for o in out if o["item_kind"] == "precheck"][0]
        self.assertEqual(pre["kind"], "sent_now")
        self.assertIsNone(pre["at"])

    def test_failed_publish_is_retried_next_pass(self):
        db = self.fresh_db("s4.db")
        watcher.schedule_pass(db, cfg_with(), [EVENTS[0]], at(15), publish=Recorder(ok=False))
        rec = Recorder()
        watcher.schedule_pass(db, cfg_with(), [EVENTS[0]], at(15, 10), publish=rec)
        self.assertTrue(rec.calls)

    def test_detail_levels(self):
        db = self.fresh_db("s5.db")
        rec = Recorder()
        watcher.schedule_pass(db, cfg_with("minimal"), [EVENTS[0]], at(15), publish=rec)
        c = [x for x in rec.calls if x["at"] == int(at(16).timestamp())][0]
        self.assertNotIn("Walk", c["body"] + c["title"])
        self.assertIsNone(c["verdict"])  # no coloured tag / priority
        self.assertIn("as of", c["body"])
        db = self.fresh_db("s6.db")
        rec = Recorder()
        watcher.schedule_pass(db, cfg_with("full"), [EVENTS[0]], at(15), publish=rec)
        c = [x for x in rec.calls if x["at"] == int(at(16).timestamp())][0]
        self.assertIn("Evening walk", c["title"])
        self.assertTrue(any(w in c["body"] for w in ("HRV", "Readiness", "slept", "Resting")), c["body"])

    def test_no_topic_no_scheduling(self):
        c = cfg_with()
        c["notify"].pop("ntfy_topic")
        rec = Recorder()
        orig = nt.SECRETS_DIR
        nt.SECRETS_DIR = os.path.join(self.tmp, "empty-secrets")
        try:
            self.assertEqual(watcher.schedule_pass(self.fresh_db("s7.db"), c, EVENTS, at(15), publish=rec), [])
        finally:
            nt.SECRETS_DIR = orig
        self.assertEqual(rec.calls, [])


class TestLiveVsScheduled(Base):
    def setUp(self):
        self.live = []
        self._orig = watcher.notify_ntfy
        watcher.notify_ntfy = lambda topic, t, b, v, server="", at=None: self.live.append((t, b, v, at)) or True

    def tearDown(self):
        watcher.notify_ntfy = self._orig

    def test_live_pass_does_not_double_send_scheduled_item(self):
        db = self.fresh_db("l1.db")
        rec = Recorder()
        kw = dict(config=cfg_with(), db_path=db, events=[EVENTS[0]], sync_fn=lambda recent: {"ok": True},
                  publish=rec)
        watcher.run_once(now=at(15), **kw)  # Mac awake at 15:00 -> phone gets the 16:00 precheck scheduled
        self.assertEqual(len([c for c in rec.calls if c["at"]]), 1)
        out = watcher.run_once(now=at(16, 2), **kw)  # Mac awake again at 16:02: live check is due
        self.assertEqual([o["kind"] for o in out], ["precheck"])
        self.assertEqual(self.live, [])  # phone already has it with this verdict
        self.assertEqual(watcher.run_once(now=at(16, 12), **kw), [])  # live dedupe still works

    def test_live_pass_sends_update_when_verdict_changed(self):
        db = self.fresh_db("l2.db")
        rec = Recorder()
        kw = dict(config=cfg_with(), db_path=db, events=[EVENTS[0]], sync_fn=lambda recent: {"ok": True},
                  publish=rec)
        watcher.run_once(now=at(15), **kw)
        key = "sched:precheck@w1@%s" % at(17).isoformat()
        conn = sqlite3.connect(db)
        real = conn.execute("SELECT verdict FROM notified WHERE event_id=?", (key,)).fetchone()[0]
        conn.execute("UPDATE notified SET verdict=? WHERE event_id=?", ("green" if real != "green" else "red", key))
        conn.commit()
        conn.close()
        rec.calls[:] = []
        watcher.run_once(now=at(16, 2), **kw)
        self.assertEqual(len(rec.calls), 1)
        self.assertTrue(rec.calls[0]["body"].startswith("Update: Walk 5:00 pm"))
        self.assertIsNone(rec.calls[0]["at"])

    def test_unscheduled_live_item_still_goes_to_phone(self):
        db = self.fresh_db("l3.db")
        out = watcher.run_once(now=at(16, 2), config=cfg_with(), db_path=db, events=[EVENTS[0]],
                               sync_fn=lambda recent: {"ok": True}, publish=Recorder())
        self.assertEqual(out[0]["kind"], "precheck")
        self.assertEqual(len(self.live), 1)
        t, b, v, _ = self.live[0]
        self.assertTrue(b.startswith("Walk 5:00 pm — "))
        self.assertEqual(t, "PlanWell")

    def test_injected_send_never_schedules(self):
        db = self.fresh_db("l4.db")
        out = watcher.run_once(now=at(15), config=cfg_with(), db_path=db, events=EVENTS,
                               sync_fn=lambda recent: {"ok": True}, send=lambda *a: True)
        self.assertFalse([o for o in out if o["kind"] in ("scheduled", "sent_now")])


class TestPublishing(unittest.TestCase):
    def test_request_headers(self):
        req = nt.build_request(TOPIC, "PlanWell — tomorrow", "body", "red", at=1700000000, token="tk_abc")
        h = {k.lower(): v for k, v in req.header_items()}
        self.assertEqual(req.full_url, "https://ntfy.sh/" + TOPIC)
        self.assertEqual(h["at"], "1700000000")
        self.assertEqual(h["priority"], "high")
        self.assertEqual(h["tags"], "red_circle")
        self.assertEqual(h["click"], "http://127.0.0.1:8765/")
        self.assertEqual(h["authorization"], "Bearer tk_abc")
        self.assertTrue(h["title"].startswith("=?UTF-8?B?"))
        h2 = {k.lower(): v for k, v in nt.build_request(TOPIC, "t", "b", "yellow").header_items()}
        self.assertEqual((h2["priority"], h2["tags"]), ("default", "yellow_circle"))
        self.assertNotIn("authorization", h2)
        self.assertNotIn("at", h2)
        with self.assertRaises(ValueError):
            nt.build_request(TOPIC, "t", "b", server="http://ntfy.sh")

    def test_token_sources(self):
        d = tempfile.mkdtemp()
        try:
            old = os.environ.pop("NTFY_TOKEN", None)
            self.assertIsNone(nt.load_token(d))
            with open(os.path.join(d, "ntfy_token.txt"), "w") as f:
                f.write("tk_file\n")
            self.assertEqual(nt.load_token(d), "tk_file")
            os.environ["NTFY_TOKEN"] = "tk_env"
            self.assertEqual(nt.load_token(d), "tk_env")
        finally:
            os.environ.pop("NTFY_TOKEN", None)
            if old is not None:
                os.environ["NTFY_TOKEN"] = old
            shutil.rmtree(d, ignore_errors=True)

    def test_publish_uses_opener_and_never_raises(self):
        seen = []

        class Resp(object):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b"{}"

        ok = nt.publish(TOPIC, "t", "b", "green", at=1, token="tk", opener=lambda req, timeout: seen.append(req) or Resp())
        self.assertTrue(ok)
        self.assertEqual(seen[0].get_header("Authorization"), "Bearer tk")

        def boom(req, timeout):
            raise OSError("offline")
        self.assertFalse(nt.publish(TOPIC, "t", "b", opener=boom, token=""))

    def test_render_levels(self):
        self.assertEqual(nt.render("full", "T", "B", "S", "SB", "red"), ("T", "B", "red"))
        self.assertEqual(nt.render("summary", "T", "B", "S", "SB", "red"), ("S", "SB", "red"))
        self.assertEqual(nt.render("summary", "T", "B", None, None, "red"), (nt.MINIMAL_TITLE, nt.MINIMAL_BODY, None))
        self.assertEqual(nt.render("minimal", "T", "B", "S", "SB", "red"), (nt.MINIMAL_TITLE, nt.MINIMAL_BODY, None))
        self.assertEqual(nt.summary_line("Padel", "7:00 pm", "red"), "Padel 7:00 pm — Go gently")


class TestTopicSecrets(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="planwell-topic-")
        self.cfg = os.path.join(self.tmp, "config.json")
        self.sdir = os.path.join(self.tmp, "secrets")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_mask(self):
        self.assertEqual(nt.mask_topic(TOPIC), "••••cdef")
        self.assertTrue(nt.is_masked(nt.mask_topic(TOPIC)))
        self.assertEqual(nt.mask_topic(""), "")

    def test_migrates_topic_out_of_config(self):
        with open(self.cfg, "w") as f:
            json.dump({"hr_threshold_bpm": 121, "notify": {"ntfy_topic": TOPIC, "macos": False}}, f)
        cfg = settings.load_config(self.cfg)
        self.assertNotIn("ntfy_topic", cfg["notify"])
        self.assertTrue(cfg["notify"]["ntfy_configured"])
        with open(self.cfg) as f:
            on_disk = json.load(f)
        self.assertNotIn("ntfy_topic", on_disk["notify"])
        self.assertFalse(on_disk["notify"]["macos"])
        self.assertEqual(on_disk["hr_threshold_bpm"], 121)
        self.assertEqual(nt.load_topic(self.sdir), TOPIC)
        p = os.path.join(self.sdir, "ntfy.json")
        self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
        pub = settings.public_config(cfg, self.cfg)
        self.assertEqual(pub["notify"]["ntfy_topic"], nt.mask_topic(TOPIC))
        self.assertTrue(pub["ntfy_configured"])
        self.assertNotIn(TOPIC, json.dumps(pub))

    def test_post_masked_keeps_new_saves_empty_clears(self):
        settings.update_config({"notify": {"ntfy_topic": TOPIC}}, self.cfg)
        self.assertEqual(nt.load_topic(self.sdir), TOPIC)
        with open(self.cfg) as f:
            self.assertNotIn(TOPIC, f.read())
        cfg = settings.update_config({"notify": {"ntfy_topic": nt.mask_topic(TOPIC), "macos": False,
                                                 "ntfy_configured": True}}, self.cfg)
        self.assertEqual(nt.load_topic(self.sdir), TOPIC)  # UI echoing the mask changes nothing
        self.assertFalse(cfg["notify"]["macos"])
        new = "another_private_topic_42"
        settings.update_config({"notify": {"ntfy_topic": new}}, self.cfg)
        self.assertEqual(nt.load_topic(self.sdir), new)
        cfg = settings.update_config({"notify": {"ntfy_topic": ""}}, self.cfg)
        self.assertIsNone(nt.load_topic(self.sdir))
        self.assertFalse(cfg["notify"]["ntfy_configured"])
        with self.assertRaises(ValueError):
            settings.update_config({"notify": {"ntfy_topic": "short"}}, self.cfg)

    def test_detail_validation_and_default(self):
        self.assertEqual(settings.DEFAULT_CONFIG["notify"]["ntfy_detail"], "summary")
        for d in ("minimal", "summary", "full"):
            settings.validate_partial({"notify": {"ntfy_detail": d}})
        with self.assertRaises(ValueError) as cm:
            settings.validate_partial({"notify": {"ntfy_detail": "everything"}})
        self.assertTrue(str(cm.exception).startswith("notify.ntfy_detail:"))

    def test_server_settings_masked(self):
        settings.update_config({"notify": {"ntfy_topic": TOPIC}}, self.cfg)
        app = server.App(mock=True, config_path=self.cfg, db_path=os.path.join(self.tmp, "x.db"))
        pub = app.public_settings()
        self.assertEqual(pub["notify"]["ntfy_topic"], nt.mask_topic(TOPIC))
        st, body = app.api_settings_post({"caution_style": "cautious"})
        self.assertEqual(st, 200)
        self.assertNotIn(TOPIC, json.dumps(body))
        st, today = app.api_today()
        self.assertNotIn(TOPIC, json.dumps(today))

    def test_notify_setup_generates_and_tests(self):
        from planwell import notify_setup
        sent = []
        pub = lambda *a, **k: sent.append(a) or True  # noqa: E731
        self.assertEqual(notify_setup.main(["--test"], secrets_dir=self.sdir, publish=pub), 1)  # no topic yet
        self.assertEqual(notify_setup.main([], secrets_dir=self.sdir, publish=pub), 0)
        t = nt.load_topic(self.sdir)
        self.assertEqual(len(t), 32)
        self.assertTrue(nt.valid_topic(t))
        self.assertEqual(sent[-1][0], t)
        self.assertEqual(notify_setup.main(["--keep"], secrets_dir=self.sdir, publish=pub), 0)
        self.assertEqual(nt.load_topic(self.sdir), t)
        self.assertEqual(notify_setup.main(["--test"], secrets_dir=self.sdir, publish=pub), 0)


class TestRepoHygiene(unittest.TestCase):
    def test_config_example_and_gitignore(self):
        with open(os.path.join(REPO, "config.example.json")) as f:
            ex = json.load(f)
        self.assertNotIn("ntfy_topic", json.dumps(ex))
        settings.validate_registry(settings.registry(ex))
        with open(os.path.join(REPO, ".gitignore")) as f:
            self.assertIn("config.json", [l.strip() for l in f])
        d = tempfile.mkdtemp()
        try:
            cfg = settings.load_config(os.path.join(d, "config.json"))  # missing -> defaults
            self.assertEqual(cfg["hr_threshold_bpm"], 125)
            self.assertFalse(os.path.exists(os.path.join(d, "config.json")))
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_server_binds_localhost_only(self):
        httpd = server.make_server(server.App(mock=True, config_path=os.path.join(tempfile.mkdtemp(), "c.json")), 0)
        try:
            self.assertEqual(httpd.server_address[0], "127.0.0.1")
        finally:
            httpd.server_close()


if __name__ == "__main__":
    unittest.main()
