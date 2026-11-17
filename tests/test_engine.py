"""PlanWell engine / API tests (stdlib unittest). Run: python3 -m unittest discover -s tests -v"""
import copy
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fixture  # noqa: E402
from planwell import classify as classify_mod  # noqa: E402
from planwell import engine, nightly, server, watcher  # noqa: E402
from planwell.settings import DEFAULT_CONFIG, save_config  # noqa: E402

TZ = fixture.TZ
CFG = copy.deepcopy(DEFAULT_CONFIG)

TODAY_KEYS = {"generated_at", "mode", "freshness", "body", "day_status", "day_headline", "upcoming", "settings"}
TODAY_EXTRA_OK = {"calendar_connected", "calendar_message"}
FRESH_KEYS = {"last_sync", "latest_hr_sample", "minutes_old"}
BODY_KEYS = {"readiness", "sleep_score", "total_sleep_min", "hrv", "hrv_baseline", "hrv_delta_pct", "rhr",
             "rhr_baseline", "rhr_delta", "stress_summary", "steps_so_far", "temperature_deviation",
             "readiness_baseline", "total_sleep_baseline_min"}
CHECK_KEYS = {"event_id", "title", "start", "end", "duration_min", "activity", "check_due", "status", "verdict",
              "confidence", "headline", "reasons", "expected", "similar_days", "options",
              "label", "cancel_by", "kind", "activity_kind"}
TOMORROW_KEYS = {"date", "generated_at", "day_status", "kind", "headline", "note", "preview_due", "events"}
OPTION_TYPES = ("shorten", "swap", "move", "buffer", "go", "rebook", "modify")
EXPECTED_KEYS = {"minutes_above_threshold", "typical_minutes_above_threshold", "peak_hr"}
SIMILAR_KEYS = {"workout_id", "date", "duration_min", "avg_hr", "peak_hr", "minutes_above_threshold", "readiness",
                "hrv_delta_pct", "similarity", "activity"}
OPTION_KEYS = {"type", "label", "detail", "new_start", "new_end"}
SESSION_KEYS = {"workout_id", "date", "start", "activity", "duration_min", "avg_hr", "peak_hr",
                "minutes_above_threshold", "minutes_per_zone", "recovery_drop_bpm", "readiness", "hrv_delta_pct"}
SESSION_EXTRA_OK = {"end", "hr_coverage", "title", "kind"}
SEVERITY = {"green": 0, "yellow": 1, "red": 2}


def at(h, m=0, day=None):
    d = day or fixture.TODAY
    return datetime(d.year, d.month, d.day, h, m, tzinfo=TZ)


def ev(title, h, m=0, dur=30, eid=None):
    s = at(h, m)
    return {"id": eid or "e-%s-%d%02d" % (title.replace(" ", ""), h, m), "title": title,
            "start": s.isoformat(), "end": (s + timedelta(minutes=dur)).isoformat(), "location": ""}


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="planwell-test-")
        cls.low_db = fixture.build(os.path.join(cls.tmp, "low.db"), today_low=True,
                                   latest_hr_at=at(14, 50))
        cls.good_db = fixture.build(os.path.join(cls.tmp, "good.db"), today_low=False,
                                    latest_hr_at=at(14, 50))
        cls.few_db = fixture.build(os.path.join(cls.tmp, "few.db"), max_workouts=3)
        cls.empty_db = os.path.join(cls.tmp, "empty.db")
        import sqlite3
        c = sqlite3.connect(cls.empty_db)
        c.executescript(fixture.SCHEMA)
        c.close()
        engine.clear_cache()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def assertKeys(self, d, required, extra_ok=frozenset()):
        keys = set(d.keys())
        self.assertTrue(required <= keys, "missing keys: %s" % (required - keys))
        self.assertTrue(keys <= set(required) | set(extra_ok), "unexpected keys: %s" % (keys - required - extra_ok))


# ------------------------------------------------------------------ classify
class TestClassify(unittest.TestCase):
    def test_keywords(self):
        c = lambda t, **kw: classify_mod.classify(dict({"title": t, "start": "2026-02-20T09:00:00-05:00"}, **kw), CFG)
        self.assertEqual(c("Morning Walk"), "walking")
        self.assertEqual(c("walking w/ Mom"), "walking")
        self.assertEqual(c("Hiking at the park"), "walking")
        self.assertEqual(c("Reformer class"), "pilates")
        self.assertEqual(c("PILATES w/ Sara"), "pilates")
        self.assertEqual(c("Solidcore class"), "pilates")
        self.assertEqual(c("BA780 class"), "class")
        self.assertEqual(c("Padel w/ Ana"), "padel")
        self.assertIsNone(c("Sidewalk chalk"))
        self.assertIsNone(c("Dentist"))
        self.assertIsNone(c("Walk", declined=True))
        self.assertIsNone(c("Walk", attendees=[{"self": True, "responseStatus": "declined"}]))
        self.assertIsNone(classify_mod.classify({"title": "Walk", "start": "2026-02-20"}, CFG))
        self.assertIsNone(classify_mod.classify({"summary": "Walk", "start": {"date": "2026-02-20"}}, CFG))
        self.assertEqual(classify_mod.classify({"summary": "Walk", "start": {"dateTime": "2026-02-20T09:00:00Z"}}, CFG),
                         "walking")


# ------------------------------------------------------------------ metrics
class TestMetrics(unittest.TestCase):
    def test_compute_metrics(self):
        st = datetime(2026, 2, 1, 14, 0, tzinfo=timezone.utc)
        en = st + timedelta(minutes=20)
        samples = []
        for k in range(20):  # 10 min at 110, 10 min at 130 (one sample per minute)
            samples.append((st + timedelta(minutes=k, seconds=5), 110 if k < 10 else 130))
        samples.append((en + timedelta(minutes=10), 95))
        m = engine.compute_metrics(samples, st, en, 125, [100, 115, 125, 140])
        self.assertEqual(m["duration_min"], 20)
        self.assertEqual(m["minutes_above_threshold"], 10)
        self.assertEqual(m["minutes_per_zone"], [0, 10, 0, 10, 0])
        self.assertEqual(m["peak_hr"], 130)
        self.assertAlmostEqual(m["avg_hr"], 120)
        self.assertEqual(m["recovery_drop_bpm"], 35)
        self.assertEqual(m["hr_coverage"], 1.0)
        self.assertTrue(m["usable"])

    def test_sparse_and_missing(self):
        st = datetime(2026, 2, 1, 14, 0, tzinfo=timezone.utc)
        en = st + timedelta(minutes=30)
        sparse = [(st + timedelta(minutes=k), 130) for k in range(0, 30, 5)]
        m = engine.compute_metrics(sparse, st, en, 125, [100, 115, 125, 140])
        self.assertLess(m["hr_coverage"], 0.3)
        self.assertEqual(m["minutes_above_threshold"], 30)  # gaps <= 5 min are filled
        self.assertIsNone(m["recovery_drop_bpm"])
        none = engine.compute_metrics([], st, en, 125, [100, 115, 125, 140])
        self.assertIsNone(none["avg_hr"])
        self.assertFalse(none["usable"])
        self.assertEqual(none["minutes_per_zone"], [0, 0, 0, 0, 0])


# ------------------------------------------------------------------ engine
class TestEngine(Base):
    def test_day_features_baselines(self):
        st = engine.get_store(self.low_db)
        f = st.day_features(fixture.TODAY.isoformat(), partial=True)
        self.assertEqual(f["readiness"], 52)
        self.assertAlmostEqual(f["total_sleep_min"], 330)
        self.assertEqual(f["hrv_baseline"], 50.0)  # median of trailing 30 (mostly good) nights; naps ignored
        self.assertAlmostEqual(f["hrv_delta_pct"], -30.0)
        self.assertEqual(f["rhr_delta"], 7)
        self.assertEqual(f["steps"], 3000)

    def test_low_day_is_cautious(self):
        c = engine.check(self.low_db, CFG, "pilates", at(18, 30), 50, now=at(15))
        self.assertKeys(c, CHECK_KEYS)
        self.assertIn(c["verdict"], ("yellow", "red"))
        self.assertIn(c["confidence"], ("moderate", "good"))  # 13 pilates sessions -> moderate
        self.assertTrue(2 <= len(c["reasons"]) <= 4)
        self.assertTrue(any("HRV" in r for r in c["reasons"]))
        self.assertGreater(c["expected"]["minutes_above_threshold"], c["expected"]["typical_minutes_above_threshold"])
        self.assertTrue(5 <= len(c["similar_days"]) <= 8)
        types = [o["type"] for o in c["options"]]
        # yellow: "go" first; red: gentle options first and "go" last
        self.assertEqual(types[0] if c["verdict"] == "yellow" else types[-1], "go")
        self.assertIn("shorten", types)
        self.assertIn("swap", types)
        self.assertIn("buffer", types)
        sh = [o for o in c["options"] if o["type"] == "shorten"][0]
        n = int(sh["label"].split()[2])
        self.assertTrue(10 <= n < 50)
        self.assertEqual(sh["new_start"], at(18, 30).isoformat())
        self.assertIn("mat", [o for o in c["options"] if o["type"] == "swap"][0]["label"].lower())
        # similar days should mostly be low days
        lows = sum(1 for s in c["similar_days"] if s["readiness"] == 52)
        self.assertGreaterEqual(lows, len(c["similar_days"]) // 2)

    def test_good_day_is_green(self):
        c = engine.check(self.good_db, CFG, "walking", at(9), 30, now=at(7))
        self.assertEqual(c["verdict"], "green")
        self.assertEqual(c["confidence"], "good")
        self.assertEqual(c["options"][0]["type"], "go")
        self.assertNotIn("shorten", [o["type"] for o in c["options"]])

    def test_status_upcoming_vs_checked(self):
        c1 = engine.check(self.good_db, CFG, "walking", at(17), 30, now=at(15))
        self.assertEqual(c1["status"], "upcoming")
        self.assertEqual(c1["check_due"], at(16).isoformat())
        c2 = engine.check(self.good_db, CFG, "walking", at(17), 30, now=at(16, 5))
        self.assertEqual(c2["status"], "checked")

    def test_move_earlier(self):
        c = engine.check(self.low_db, CFG, "walking", at(17), 30, now=at(7))
        mv = [o for o in c["options"] if o["type"] == "move"]
        self.assertTrue(mv, "expected a move-earlier option: %s" % c["options"])
        ns = datetime.fromisoformat(mv[0]["new_start"])
        self.assertTrue(at(7, 30) <= ns < at(17))
        self.assertEqual(datetime.fromisoformat(mv[0]["new_end"]) - ns, timedelta(minutes=30))
        # if the earlier window has passed, no move option
        c2 = engine.check(self.low_db, CFG, "walking", at(17), 30, now=at(15))
        self.assertFalse([o for o in c2["options"] if o["type"] == "move"])

    def test_caution_styles_ordered(self):
        sev = []
        for style in ("cautious", "balanced", "flexible"):
            cfg = dict(CFG, caution_style=style)
            sev.append(SEVERITY[engine.check(self.low_db, cfg, "walking", at(17), 30, now=at(15))["verdict"]])
        self.assertTrue(sev[0] >= sev[1] >= sev[2], sev)
        self.assertEqual(sev[0], 2)

    def test_learning_fallback(self):
        c = engine.check(self.few_db, CFG, "pilates", at(18, 30), 50, now=at(15))
        self.assertEqual(c["confidence"], "learning")
        self.assertTrue(c["reasons"][0].startswith("Still learning"))
        self.assertTrue(len(c["similar_days"]) <= 5)
        self.assertIn(c["verdict"], ("yellow", "red"))  # rules: low readiness + HRV drop

    def test_missing_everything(self):
        t = engine.build_today(self.empty_db, CFG, [ev("Walk", 17)], now=at(15))
        self.assertTrue(all(v is None for v in t["body"].values()))
        self.assertEqual(t["day_status"], "yellow")
        self.assertEqual(len(t["upcoming"]), 1)
        self.assertEqual(t["upcoming"][0]["confidence"], "learning")
        self.assertIsNone(t["freshness"]["latest_hr_sample"])
        t2 = engine.build_today(os.path.join(self.tmp, "does-not-exist.db"), CFG, [], now=at(15))
        self.assertEqual(t2["upcoming"], [])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "does-not-exist.db")))

    def test_build_today_shape(self):
        events = [ev("Coffee", 16), ev("Pilates w/ Sara", 18, 30, 50), ev("Evening walk", 17),
                  ev("Morning walk", 8)]  # past -> dropped
        t = engine.build_today(self.low_db, CFG, events, now=at(15), mode="mock")
        self.assertKeys(t, TODAY_KEYS, TODAY_EXTRA_OK)
        self.assertKeys(t["freshness"], FRESH_KEYS)
        self.assertKeys(t["body"], BODY_KEYS)
        self.assertEqual(t["mode"], "mock")
        self.assertEqual(t["freshness"]["minutes_old"], 10)
        self.assertIn(t["day_status"], ("green", "yellow", "red"))
        self.assertEqual([c["title"] for c in t["upcoming"]], ["Evening walk", "Pilates w/ Sara"])
        for c in t["upcoming"]:
            self.assertKeys(c, CHECK_KEYS)
            self.assertKeys(c["expected"], EXPECTED_KEYS)
            for s in c["similar_days"]:
                self.assertKeys(s, SIMILAR_KEYS)
            for o in c["options"]:
                self.assertKeys(o, OPTION_KEYS)
                self.assertIn(o["type"], OPTION_TYPES)
            self.assertIn(c["verdict"], ("green", "yellow", "red"))
            self.assertIn(c["confidence"], ("learning", "moderate", "good"))
        json.dumps(t)

    def test_sessions_and_detail(self):
        s = engine.sessions(self.low_db, CFG, activity="pilates", limit=5)
        self.assertEqual(set(s.keys()), {"sessions", "zones_bpm", "hr_threshold_bpm"})
        self.assertEqual(len(s["sessions"]), 5)
        for row in s["sessions"]:
            self.assertKeys(row, SESSION_KEYS, SESSION_EXTRA_OK)
            self.assertEqual(row["activity"], "pilates")
            self.assertEqual(len(row["minutes_per_zone"]), 5)
            self.assertEqual(sum(row["minutes_per_zone"]), row["duration_min"])
        dates = [r["date"] for r in s["sessions"]]
        self.assertEqual(dates, sorted(dates, reverse=True))
        both = engine.sessions(self.low_db, CFG)
        self.assertEqual({r["activity"] for r in both["sessions"]}, {"walking", "pilates"})  # no padel/class here
        d = engine.session_detail(self.low_db, CFG, s["sessions"][0]["workout_id"])
        self.assertEqual(set(d.keys()), {"session", "hr"})
        self.assertKeys(d["hr"][0], {"t", "bpm"})
        self.assertGreater(len(d["hr"]), 100)
        self.assertIsNone(engine.session_detail(self.low_db, CFG, "nope"))


# ------------------------------------------------------------------ v2: labels, Solidcore, preview
class TestV2(Base):
    def test_labels_and_solidcore_wording(self):
        c = engine.check(self.low_db, CFG, "pilates", at(18, 30), 50, now=at(15))
        self.assertEqual(c["label"], "Solidcore")
        self.assertEqual(c["kind"], "precheck")
        self.assertIsNone(c["cancel_by"])  # late_cancel_hours unknown -> never assumed
        text = " ".join([c["headline"]] + c["reasons"] + [o["label"] + " " + o["detail"] for o in c["options"]])
        self.assertNotIn("pilates", text.lower())
        self.assertIn("Solidcore", text)
        w = engine.check(self.low_db, CFG, "walking", at(17), 30, now=at(15))
        self.assertEqual(w["label"], "Walk")
        self.assertTrue(any("walk sessions" in r for r in w["reasons"]))
        self.assertTrue(engine.check(self.low_db, CFG, "pilates", at(18), 50, now=at(15))["title"]
                        .startswith("Solidcore"))
        cfg = dict(CFG, activity_labels={"walking": "Stroll", "pilates": "Core class"})
        self.assertEqual(engine.check(self.low_db, cfg, "pilates", at(18), 50, now=at(15))["label"], "Core class")

    def test_solidcore_options(self):
        c = engine.check(self.low_db, CFG, "pilates", at(18, 30), 50, now=at(15))
        types = [o["type"] for o in c["options"]]
        self.assertIn("modify", types)
        self.assertIn("rebook", types)
        self.assertNotIn("move", types)
        mod = [o for o in c["options"] if o["type"] == "modify"][0]
        self.assertEqual(mod["label"], "Plan floor modifications")
        self.assertIn("standing transitions", mod["detail"])
        order = [t for t in engine.OPTION_ORDER if t in types and t != "go"]
        self.assertEqual([t for t in types if t != "go"], order)  # least disruptive first
        if c["verdict"] == "red":
            self.assertEqual(types[-1], "go")
        w = engine.check(self.low_db, CFG, "walking", at(17), 30, now=at(7))
        wt = [o["type"] for o in w["options"]]
        self.assertFalse({"modify", "rebook"} & set(wt))
        self.assertIn("move", wt)

    def test_rebook_later_when_history_says_so(self):
        # fixture pilates is always 18:30; a 9:00 class has no morning history -> generic rebook
        c = engine.check(self.low_db, CFG, "pilates", at(9), 50, now=at(7))
        rb = [o for o in c["options"] if o["type"] == "rebook"]
        if rb:
            self.assertEqual(rb[0]["label"], "Book a later class")
            self.assertIsNone(rb[0]["new_start"])

    def test_cancel_by(self):
        cfg = dict(CFG, late_cancel_hours={"pilates": 12, "walking": None})
        start = at(9) + timedelta(days=1)
        c = engine.check(self.low_db, cfg, "pilates", start, 50, now=at(15))
        self.assertEqual(c["cancel_by"], (start - timedelta(hours=12)).isoformat())
        self.assertEqual(c["kind"], "preview")
        self.assertTrue(c["headline"].startswith("Early look"))
        if c["verdict"] != "green":
            self.assertTrue(any("late-cancel" in r for r in c["reasons"]))
        late = engine.check(self.low_db, cfg, "pilates", start, 50, now=at(22))
        if late["verdict"] != "green":
            self.assertTrue(any("already passed" in r for r in late["reasons"]))
        self.assertTrue(len(late["reasons"]) <= 4)

    def test_preview_due(self):
        start = at(7) + timedelta(days=1)
        self.assertEqual(engine.preview_due_for(start, "pilates", CFG, at(15)), at(20))
        cfg = dict(CFG, late_cancel_hours={"pilates": 12, "walking": None})
        self.assertEqual(engine.preview_due_for(start, "pilates", cfg, at(15)), at(18))
        self.assertEqual(engine.preview_due_for(start, "walking", cfg, at(15)), at(20))
        cfg2 = dict(CFG, preview={"enabled": True, "time": "19:30"})
        self.assertEqual(engine.preview_due_for(start, "walking", cfg2, at(15)), at(19, 30))

    def test_build_tomorrow_shape(self):
        d1 = fixture.TODAY + timedelta(days=1)
        evs = [ev("Pilates now", 18, 30, 50),
               {"id": "t1", "title": "Morning walk", "start": at(8, 0, d1).isoformat(),
                "end": at(8, 30, d1).isoformat()},
               {"id": "t2", "title": "Solidcore", "start": at(7, 0, d1).isoformat(),
                "end": at(7, 50, d1).isoformat()},
               {"id": "t3", "title": "Dentist", "start": at(10, 0, d1).isoformat(),
                "end": at(11, 0, d1).isoformat()}]
        cfg = dict(CFG, late_cancel_hours={"pilates": 12, "walking": None})
        t = engine.build_tomorrow(self.low_db, cfg, evs, now=at(15))
        self.assertKeys(t, TOMORROW_KEYS)
        self.assertEqual(t["date"], d1.isoformat())
        self.assertEqual(t["kind"], "preview")
        self.assertEqual([c["event_id"] for c in t["events"]], ["t2", "t1"])
        self.assertEqual(t["preview_due"], at(18).isoformat())
        self.assertIn("1 hour", t["note"])
        self.assertTrue(t["headline"].startswith("Tomorrow"))
        worst = max(t["events"], key=lambda c: SEVERITY[c["verdict"]])["verdict"]
        self.assertEqual(t["day_status"], worst)
        for c in t["events"]:
            self.assertKeys(c, CHECK_KEYS)
            self.assertEqual(c["kind"], "preview")
            self.assertNotEqual(c["confidence"], "good")
        empty = engine.build_tomorrow(self.low_db, CFG, [], now=at(15))
        self.assertEqual(empty["events"], [])
        self.assertIn("Nothing tracked", empty["headline"])
        json.dumps(t)


# ------------------------------------------------------------------ v3: registry, padel, classes, next48
NEXT48_DAY_KEYS = {"date", "label", "load", "headline", "items", "suggestions"}
NEXT48_ITEM_KEYS = {"event_id", "title", "start", "end", "activity", "label", "check", "ai_label", "tracked"}


class TestSettingsV3(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="planwell-cfg-")
        self.path = os.path.join(self.tmp, "config.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_migrates_legacy_config(self):
        from planwell import settings as S
        legacy = {"hr_threshold_bpm": 120,
                  "activity_keywords": {"walking": ["walk", "steps"], "pilates": ["solidcore", "mat class"]},
                  "activity_labels": {"walking": "Walk", "pilates": "Solidcore"},
                  "late_cancel_hours": {"pilates": 12, "walking": None}}
        with open(self.path, "w") as f:
            json.dump(legacy, f)
        cfg = S.load_config(self.path)
        self.assertEqual(list(cfg["activities"]), ["walking", "pilates", "padel", "class"])
        self.assertEqual(cfg["activities"]["pilates"]["keywords"], ["solidcore", "mat class"])
        self.assertEqual(cfg["activities"]["pilates"]["late_cancel_hours"], 12)
        self.assertEqual(cfg["activities"]["class"]["kind"], "daily")
        self.assertEqual(cfg["late_cancel_hours"]["pilates"], 12)  # legacy view still readable
        self.assertEqual(cfg["hr_threshold_bpm"], 120)
        S.save_config(cfg, self.path)
        with open(self.path) as f:
            saved = json.load(f)
        self.assertIn("activities", saved)
        self.assertFalse(set(S.LEGACY_KEYS) & set(saved))

    def test_add_edit_remove_and_validate(self):
        from planwell import settings as S
        S.save_config(copy.deepcopy(S.DEFAULT_CONFIG), self.path)
        cfg = S.update_config({"activities": {
            "yoga": {"label": "Yoga", "keywords": ["yoga"],
                     "traits": {"posture": "floor", "pattern": "steady", "setting": "indoor"}},
            "padel": {"late_cancel_hours": 24},
            "class": None}}, self.path)
        self.assertEqual(list(cfg["activities"]), ["walking", "pilates", "padel", "yoga"])
        self.assertEqual(cfg["activities"]["yoga"]["oura_types"], ["yoga"])
        self.assertEqual(cfg["activities"]["padel"]["late_cancel_hours"], 24)
        self.assertEqual(cfg["activities"]["padel"]["keywords"], ["padel"])  # edit keeps other fields
        self.assertEqual(S.load_config(self.path)["activities"]["yoga"]["label"], "Yoga")
        # legacy-shaped partial updates still work
        cfg = S.update_config({"activity_labels": {"walking": "Stroll"}}, self.path)
        self.assertEqual(cfg["activities"]["walking"]["label"], "Stroll")
        for bad in ({"activities": {"Bad Key": {"keywords": ["x"]}}},
                    {"activities": {"x": {"kind": "sport", "keywords": ["x"]}}},
                    {"activities": {"x": {"keywords": []}}},
                    {"activities": {"walking": {"traits": {"posture": "lying"}}}},
                    {"activities": {"walking": {"late_cancel_hours": 99}}},
                    {"activities": {k: None for k in ["walking", "pilates", "padel", "yoga"]}},
                    {"morning": {"time": "7am"}},
                    {"labeler": {"provider": "openai"}}):
            with self.assertRaises(ValueError, msg=str(bad)):
                S.update_config(bad, self.path)
        self.assertEqual(list(S.load_config(self.path)["activities"]), ["walking", "pilates", "padel", "yoga"])

    def test_validate_partial_field_paths(self):
        from planwell import settings as S
        cases = [
            ({"activities": {"Padel!": {}}}, "activities.Padel!:"),
            ({"activities": {"padel": []}}, "activities.padel:"),
            ({"activities": {"padel": {"label": ""}}}, "activities.padel.label:"),
            ({"activities": {"padel": {"label": "x" * 41}}}, "activities.padel.label:"),
            ({"activities": {"padel": {"kind": "sport"}}}, "activities.padel.kind:"),
            ({"activities": {"padel": {"keywords": []}}}, "activities.padel.keywords:"),
            ({"activities": {"padel": {"keywords": ["ok", " "]}}}, "activities.padel.keywords:"),
            ({"activities": {"padel": {"keywords": "padel"}}}, "activities.padel.keywords:"),
            ({"activities": {"padel": {"oura_types": "padel"}}}, "activities.padel.oura_types:"),
            ({"activities": {"padel": {"oura_types": [3]}}}, "activities.padel.oura_types:"),
            ({"activities": {"padel": {"traits": "bursty"}}}, "activities.padel.traits:"),
            ({"activities": {"padel": {"traits": {"posture": "lying"}}}}, "activities.padel.traits.posture:"),
            ({"activities": {"padel": {"traits": {"pattern": "fast"}}}}, "activities.padel.traits.pattern:"),
            ({"activities": {"padel": {"traits": {"setting": "space"}}}}, "activities.padel.traits.setting:"),
            ({"activities": {"padel": {"late_cancel_hours": 73}}}, "activities.padel.late_cancel_hours:"),
            ({"activities": {"padel": {"late_cancel_hours": "12"}}}, "activities.padel.late_cancel_hours:"),
            ({"activities": {"padel": {"late_cancel_hours": True}}}, "activities.padel.late_cancel_hours:"),
            ({"morning": {"time": "7am"}}, "morning.time:"),
            ({"morning": {"time": "24:00"}}, "morning.time:"),
            ({"morning": {"enabled": "yes"}}, "morning.enabled:"),
            ({"morning": "07:00"}, "morning:"),
            ({"labeler": {"provider": "openai"}}, "labeler.provider:"),
            ({"labeler": {"model": ""}}, "labeler.model:"),
            ({"labeler": {"vertex_project": 5}}, "labeler.vertex_project:"),
            ({"labeler": {"vertex_region": None}}, "labeler.vertex_region:"),
        ]
        for partial, prefix in cases:
            with self.assertRaises(ValueError, msg=str(partial)) as cm:
                S.validate_partial(partial)
            self.assertTrue(str(cm.exception).startswith(prefix), "%s -> %s" % (partial, cm.exception))
        # valid partials pass (only given fields are checked; null removes)
        S.validate_partial({"activities": {"padel": {"late_cancel_hours": 24}, "class": None},
                            "morning": {"enabled": False, "time": "06:45"},
                            "labeler": {"provider": "vertex", "model": "claude-haiku-5-5",
                                        "vertex_project": "p", "vertex_region": ""}})
        # a brand-new activity is fully validated on update (missing keywords default to its key)
        S.save_config(copy.deepcopy(S.DEFAULT_CONFIG), self.path)
        with self.assertRaises(ValueError) as cm:
            S.update_config({"activities": {"swim": {"kind": "exercise", "keywords": []}}}, self.path)
        self.assertTrue(str(cm.exception).startswith("activities.swim.keywords:"))
        with self.assertRaises(ValueError) as cm:
            S.update_config({"activities": {k: None for k in S.DEFAULT_ACTIVITIES}}, self.path)
        self.assertTrue(str(cm.exception).startswith("activities:"))

    def test_registry_from_plain_dicts(self):
        from planwell import settings as S
        reg = S.registry({"activity_keywords": {"walking": ["walk"]}})
        self.assertEqual(reg["walking"]["keywords"], ["walk"])
        self.assertIn("padel", reg)


class TestV3(Base):
    @classmethod
    def setUpClass(cls):
        super(TestV3, cls).setUpClass()
        cls.v3_db = fixture.build(os.path.join(cls.tmp, "v3.db"), today_low=True, padel=2, classes=True,
                                  latest_hr_at=at(14, 50))

    def test_classify_registry(self):
        c = lambda t: classify_mod.classify({"title": t, "start": at(9).isoformat()}, CFG)
        self.assertEqual(c("BA780 class"), "class")
        self.assertEqual(c("MSBA seminar"), "class")
        self.assertEqual(c("Walk to class"), "walking")  # earliest keyword wins
        self.assertEqual(c("Solidcore class"), "pilates")
        self.assertEqual(c("Padel w/ Ana"), "padel")
        cfg = copy.deepcopy(CFG)
        cfg["activities"]["yoga"] = {"label": "Yoga", "kind": "exercise", "keywords": ["yoga"]}
        cfg.pop("activity_keywords")
        self.assertEqual(classify_mod.classify({"title": "Hot yoga", "start": at(9).isoformat()}, cfg), "yoga")
        lab = classify_mod.rules_label({"title": "Padel", "start": at(9).isoformat()}, CFG)
        self.assertEqual((lab["category"], lab["pattern"], lab["source"]), ("padel", "bursty", "rules"))

    def test_exercise_by_oura_types_and_daily_sessions(self):
        s = engine.sessions(self.v3_db, CFG, limit=500)["sessions"]
        acts = {r["activity"] for r in s}
        self.assertEqual(acts, {"walking", "pilates", "padel", "class"})
        classes = [r for r in s if r["activity"] == "class"]
        self.assertTrue(all(r["kind"] == "daily" and r["title"] == "BA780 class" for r in classes))
        self.assertNotIn("cal-cls-nohr", [r["workout_id"] for r in classes])  # no HR -> skipped
        d = engine.session_detail(self.v3_db, CFG, classes[0]["workout_id"])
        self.assertIsNotNone(d)
        self.assertGreater(len(d["hr"]), 10)
        # Oura type mapping: "tennis" counts as padel via oura_types
        self.assertEqual(engine.norm_activity("tennis", CFG), "padel")
        self.assertEqual(engine.norm_activity("racquet_sports", CFG), "padel")

    def test_load_yesterday_feature(self):
        store = engine.get_store(self.v3_db)
        recs = engine._session_records(store, CFG)
        self.assertTrue(all("load_yesterday" in r["f"] for r in recs))
        self.assertTrue(any(r["f"]["load_yesterday"] > 0 for r in recs))
        self.assertIn("load_yesterday", [f[0] for f in engine.FEATURES])

    def test_cold_start_padel(self):
        c = engine.check(self.v3_db, CFG, "padel", at(19), 60, now=at(15))
        self.assertEqual(c["label"], "Padel")
        self.assertEqual(c["confidence"], "learning")
        self.assertEqual(c["reasons"][0], "Still learning padel \u2014 using your similar activities for now")
        self.assertIsNotNone(c["expected"]["minutes_above_threshold"])
        self.assertTrue({s_["activity"] for s_ in c["similar_days"]} - {"padel"})  # borrowed neighbours
        types = [o["type"] for o in c["options"]]
        self.assertNotIn("rebook", types)  # no late-cancel window, not a booked indoor class
        if c["verdict"] != "green":
            mod = [o for o in c["options"] if o["type"] == "modify"][0]
            self.assertEqual(mod["label"], "Plan more breaks")
            self.assertIn("slower-paced", [o for o in c["options"] if o["type"] == "swap"][0]["label"])
        cfg = copy.deepcopy(CFG)
        cfg["activities"]["padel"]["late_cancel_hours"] = 24
        cfg["late_cancel_hours"]["padel"] = 24
        c2 = engine.check(self.v3_db, dict(cfg, caution_style="cautious"), "padel", at(19), 60, now=at(15))
        if c2["verdict"] != "green":
            self.assertIn("rebook", [o["type"] for o in c2["options"]])
        self.assertEqual(c2["cancel_by"], (at(19) - timedelta(hours=24)).isoformat())

    def test_blend_weight(self):
        self.assertAlmostEqual(engine._blend(10.0, 20.0, 2 / 7.0), 10 * 2 / 7.0 + 20 * 5 / 7.0)
        self.assertEqual(engine._blend(None, 3.0, 0.5), 3.0)
        self.assertEqual(engine._blend(4.0, None, 0.0), 4.0)
        reg = engine.registry(CFG)
        self.assertEqual(engine._trait_weight(reg["padel"], reg["walking"]), 0.5)  # posture
        self.assertEqual(engine._trait_weight(reg["padel"], reg["pilates"]), 0.5)  # pattern
        self.assertEqual(engine._trait_weight(reg["class"], reg["walking"]), 0.0)

    def test_daily_kind_options(self):
        cfg = dict(CFG, caution_style="cautious")
        c = engine.check(self.v3_db, cfg, "class", at(11), 75, now=at(9))
        self.assertEqual(c["label"], "Class")
        self.assertEqual(c["activity_kind"], "daily")
        types = {o["type"] for o in c["options"]}
        self.assertTrue(types <= {"go", "buffer", "modify"}, types)
        self.assertIn("Add a rest block after", [o["label"] for o in c["options"]])
        if c["verdict"] != "green":
            self.assertIn("Plan a seated break", [o["label"] for o in c["options"]])
        text = " ".join(o["detail"] for o in c["options"]).lower()
        for banned in ("salt", "electrolyte", "compression", "medication"):
            self.assertNotIn(banned, text)

    def test_load_level(self):
        self.assertEqual(engine.load_level(5, 20), "light")
        self.assertEqual(engine.load_level(20, 20), "usual")
        self.assertEqual(engine.load_level(30, 20), "heavy")
        self.assertEqual(engine.load_level(50, 20), "very_heavy")
        self.assertEqual(engine.load_level(3, 0), "usual")  # floor + smoothing: tiny typical isn't "heavy"
        self.assertEqual(engine.load_level(2, 20, has_exercise=False), "light")

    def _n48_events(self):
        d1 = fixture.TODAY + timedelta(days=1)
        return [ev("Solidcore", 17, 0, 50, eid="s1"), ev("BA780 class", 18, 0, 75, eid="c1"),
                ev("Padel w/ Ana", 19, 30, 60, eid="p1"), ev("Dentist", 16, 0, 30, eid="x1"),
                ev("Morning walk", 8, 0, 30, eid="past"),
                {"id": "w2", "title": "Morning walk", "start": at(8, 0, d1).isoformat(), "end": at(8, 30, d1).isoformat()},
                {"id": "p2", "title": "Padel", "start": at(18, 0, d1).isoformat(), "end": at(19, 0, d1).isoformat()}]

    def test_build_next48(self):
        t = engine.build_next48(self.v3_db, CFG, self._n48_events(), now=at(15))
        self.assertEqual(set(t), {"generated_at", "days", "note"})
        self.assertEqual([d["label"] for d in t["days"]], ["Today", "Tomorrow"])
        today = t["days"][0]
        self.assertTrue(NEXT48_DAY_KEYS <= set(today))
        self.assertEqual([it["event_id"] for it in today["items"]], ["x1", "s1", "c1", "p1"])  # past dropped
        for d in t["days"]:
            self.assertIn(d["load"]["level"], engine.LEVELS)
            self.assertTrue({"expected", "typical", "level"} <= set(d["load"]))
            for it in d["items"]:
                self.assertTrue(NEXT48_ITEM_KEYS <= set(it))
                if it["tracked"]:
                    self.assertKeys(it["check"], CHECK_KEYS)
                else:
                    self.assertIsNone(it["check"])
            for sg in d["suggestions"]:
                self.assertIn(sg["type"], ("space", "rest_block", "move", "shorten"))
                self.assertIn("event_id", sg)
        dentist = today["items"][0]
        self.assertFalse(dentist["tracked"])
        self.assertEqual(dentist["ai_label"]["category"], "other")
        # Solidcore 17:00-17:50 then class at 18:00 -> rest block suggestion
        self.assertIn("rest_block", [sg["type"] for sg in today["suggestions"]])
        self.assertIn("1 hour", t["note"])
        json.dumps(t)

    def test_next48_levels_track_body_state(self):
        good = fixture.build(os.path.join(self.tmp, "v3good.db"), today_low=False, padel=2, classes=True)
        evs = self._n48_events()
        lv = lambda p: engine.LEVELS.index(engine.build_next48(p, CFG, evs, now=at(15))["days"][0]["load"]["level"])
        self.assertLessEqual(lv(good), lv(self.v3_db))
        self.assertLessEqual(lv(good), 1)  # a good day with a usual plan is light/usual

    def test_back_to_back_suggestion(self):
        # direct unit test of the suggestion builder
        item = {"event_id": "p", "activity": "padel", "label": "Padel", "tracked": True,
                "start": at(19).isoformat(), "end": at(20).isoformat(),
                "check": {"verdict": "yellow", "expected": {"minutes_above_threshold": 20}, "options": []}}
        out = engine._day_suggestions([item], set(), "heavy", "heavy", [], CFG, 125, True, TZ)
        self.assertEqual(out[0]["type"], "space")
        self.assertIn("padel", out[0]["text"])
        self.assertEqual(engine._day_suggestions([item], set(), "usual", "heavy", [], CFG, 125, True, TZ), [])


class TestNightlyAndHistory(Base):
    def test_nightly_never_raises(self):
        log_path = os.path.join(self.tmp, "nightly.log")
        calls = []

        def bad_sync(**kw):
            calls.append(kw)
            raise RuntimeError("oura down")
        out = nightly.run(db_path=os.path.join(self.tmp, "n.db"), config=CFG, sync_fn=bad_sync,
                          history_fn=lambda b, a, c, db_path=None: 7, log_path=log_path)
        self.assertEqual(calls[0]["days"], 3)
        self.assertEqual(out["calendar"], 7)
        self.assertTrue(any("oura" in e for e in out["errors"]))
        with open(log_path) as f:
            self.assertIn("Oura sync failed", f.read())

        def bad_hist(b, a, c, db_path=None):
            self.assertEqual((b, a), (14, 3))
            raise RuntimeError("no google")
        out = nightly.run(db_path=os.path.join(self.tmp, "n.db"), config=CFG,
                          sync_fn=lambda **kw: {"ok": True}, history_fn=bad_hist, log_path=log_path)
        self.assertTrue(any("calendar" in e for e in out["errors"]))

    def test_save_history(self):
        from planwell import gcal, db as dbm
        p = os.path.join(self.tmp, "hist.db")
        evs = [{"id": "e1", "calendar_id": "primary", "title": "BA780 class",
                "start": "2026-02-10T09:30:00-05:00", "end": "2026-02-10T10:45:00-05:00"},
               {"id": "e2", "calendar_id": "primary", "title": "Gone",
                "start": "2026-02-11T09:30:00-05:00", "end": "2026-02-11T10:00:00-05:00"}]
        win = ("2026-02-01T00:00:00-05:00", "2026-02-20T00:00:00-05:00")
        self.assertEqual(gcal.save_history(evs, win[0], win[1], p), 2)
        gcal.save_history(evs[:1], win[0], win[1], p)  # e2 was deleted/cancelled
        c = dbm.connect(p)
        self.assertEqual([r["id"] for r in c.execute("SELECT id FROM calendar_events")], ["e1"])
        c.close()


# ------------------------------------------------------------------ server
class TestServer(Base):
    @classmethod
    def setUpClass(cls):
        super(TestServer, cls).setUpClass()
        cls.cfg_path = os.path.join(cls.tmp, "config.json")
        save_config(CFG, cls.cfg_path)
        web = os.path.join(cls.tmp, "web")
        os.makedirs(web)
        with open(os.path.join(web, "index.html"), "w") as f:
            f.write("<!doctype html><title>t</title>hello")
        cls.app = server.App(mock=True, config_path=cls.cfg_path, db_path=cls.low_db, web_dir=web)
        cls.app.events = lambda cfg, now: ([ev("Pilates", 23, 0, 50)], True, None)
        cls.httpd = server.make_server(cls.app, 0)
        cls.base = "http://127.0.0.1:%d" % cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        super(TestServer, cls).tearDownClass()

    def req(self, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(self.base + path, data=data, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(r) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_endpoints(self):
        st, t = self.req("/api/today")
        self.assertEqual(st, 200)
        self.assertKeys(t, TODAY_KEYS, TODAY_EXTRA_OK)
        self.assertEqual(t["mode"], "mock")
        st, c = self.req("/api/check", {"activity": "walking", "start": at(23).isoformat(), "duration_min": 30})
        self.assertEqual(st, 200)
        self.assertKeys(c, CHECK_KEYS)
        st, e = self.req("/api/check", {"activity": "swimming"})
        self.assertEqual(st, 400)
        self.assertIn("error", e)
        st, tm = self.req("/api/tomorrow")
        self.assertEqual(st, 200)
        self.assertKeys(tm, TOMORROW_KEYS, TODAY_EXTRA_OK)
        st, n48 = self.req("/api/next48")
        self.assertEqual(st, 200)
        self.assertEqual(len(n48["days"]), 2)
        st, s = self.req("/api/sessions?activity=walking&limit=3")
        self.assertEqual(len(s["sessions"]), 3)
        st, d = self.req("/api/session/" + s["sessions"][0]["workout_id"])
        self.assertEqual(st, 200)
        self.assertIn("hr", d)
        st, e = self.req("/api/session/missing")
        self.assertEqual(st, 404)
        st, cfg = self.req("/api/settings")
        self.assertEqual(cfg["hr_threshold_bpm"], 125)
        st, cfg = self.req("/api/settings", {"hr_threshold_bpm": 120, "notify": {"ntfy_topic": "planwell-test-topic-0001"}})
        self.assertEqual(st, 200)
        self.assertEqual(cfg["hr_threshold_bpm"], 120)
        self.assertTrue(cfg["notify"]["macos"])  # nested merge keeps other keys
        with open(self.cfg_path) as f:
            self.assertNotIn("ntfy_topic", json.load(f)["notify"])  # secret: never in config.json
        with open(os.path.join(os.path.dirname(self.cfg_path), "secrets", "ntfy.json")) as f:
            self.assertEqual(json.load(f)["topic"], "planwell-test-topic-0001")
        self.assertEqual(cfg["notify"]["ntfy_topic"], "\u2022\u2022\u2022\u20220001")
        self.assertTrue(cfg["ntfy_configured"])
        st, e = self.req("/api/settings", {"caution_style": "reckless"})
        self.assertEqual(st, 400)
        st, e = self.req("/api/settings", {"activities": {"padel": {"traits": {"posture": "lying"}}}})
        self.assertEqual(st, 400)
        self.assertTrue(e["error"].startswith("activities.padel.traits.posture:"), e)
        st, e = self.req("/api/settings", {"morning": {"time": "25:00"}})
        self.assertEqual((st, e["error"].split(":")[0]), (400, "morning.time"))
        st, e = self.req("/api/nope")
        self.assertEqual(st, 404)
        with urllib.request.urlopen(self.base + "/") as r:
            self.assertIn(b"hello", r.read())
        try:
            urllib.request.urlopen(self.base + "/../config.json")
        except urllib.error.HTTPError as e:
            self.assertIn(e.code, (403, 404))


class TestAnalyticsEndpoints(Base):
    def test_cache_and_unavailable(self):
        import types
        cfg_path = os.path.join(self.tmp, "config_an.json")
        save_config(CFG, cfg_path)
        db_path = os.path.join(self.tmp, "an.db")
        shutil.copy(self.low_db, db_path)
        app = server.App(mock=True, config_path=cfg_path, db_path=db_path)
        calls = []
        fake = types.ModuleType("planwell.analytics")
        fake.insights = lambda p, c: calls.append("i") or {"insights": [], "n": len(calls)}
        fake.backtest = lambda p, c: calls.append("b") or {"planwell": {}, "n": len(calls)}
        saved = sys.modules.get("planwell.analytics")
        import planwell
        saved_attr = getattr(planwell, "analytics", None)
        sys.modules["planwell.analytics"] = fake
        planwell.analytics = fake
        try:
            self.assertEqual(app.api_analytics("insights")[0], 200)
            app.api_analytics("insights")
            self.assertEqual(calls, ["i"])  # cached
            app.api_analytics("backtest")
            self.assertEqual(calls, ["i", "b"])
            app.api_settings_post({"hr_threshold_bpm": 121})  # settings change invalidates
            app.api_analytics("insights")
            self.assertEqual(calls, ["i", "b", "i"])
            import sqlite3, time as _t
            _t.sleep(0.01)
            c = sqlite3.connect(db_path)
            c.execute("INSERT INTO daily_sleep VALUES ('2030-01-01', 80, '{}')")
            c.commit(); c.close()
            app.api_analytics("insights")  # DB change invalidates
            self.assertEqual(calls, ["i", "b", "i", "i"])
            del fake.insights
            st2, body2 = server.App(mock=True, config_path=cfg_path, db_path=db_path).api_analytics("insights")
            self.assertEqual(st2, 503)
            self.assertIn("error", body2)
        finally:
            if saved is not None:
                sys.modules["planwell.analytics"] = saved
            else:
                sys.modules.pop("planwell.analytics", None)
            if saved_attr is not None:
                planwell.analytics = saved_attr
            elif hasattr(planwell, "analytics"):
                del planwell.analytics


# ------------------------------------------------------------------ watcher
class TestWatcher(Base):
    def test_notify_once_and_dedupe(self):
        db_path = os.path.join(self.tmp, "watch.db")
        shutil.copy(self.low_db, db_path)
        sent = []
        events = [ev("Pilates w/ Sara", 16, 0, 50, eid="p1"), ev("Walk later", 20, 0, eid="w9"),
                  ev("Dentist", 15, 30, eid="d1")]
        kw = dict(now=at(15, 2), config=CFG, db_path=db_path, events=events,
                  send=lambda title, body, chk: sent.append((title, body)) or True,
                  sync_fn=lambda recent: {"ok": True})
        out = watcher.run_once(**kw)
        self.assertEqual([o["event_id"] for o in out], ["p1"])
        title, body = sent[0]
        self.assertIn("Pilates w/ Sara", title)
        self.assertIn("Option:", body)
        self.assertEqual(watcher.run_once(**kw), [])  # deduped
        self.assertEqual(len(sent), 1)

    def test_sync_failure_still_checks(self):
        db_path = os.path.join(self.tmp, "watch2.db")
        shutil.copy(self.low_db, db_path)
        sent = []

        def boom(recent):
            raise RuntimeError("network down")
        out = watcher.run_once(now=at(15, 2), config=CFG, db_path=db_path,
                               events=[ev("Walk", 16, 0, eid="w1")],
                               send=lambda t, b, c: sent.append(b) or True, sync_fn=boom)
        self.assertEqual(len(out), 1)
        self.assertIn("Couldn't refresh Oura", sent[0])

    def test_preview_notification(self):
        db_path = os.path.join(self.tmp, "watch3.db")
        shutil.copy(self.low_db, db_path)
        d1 = fixture.TODAY + timedelta(days=1)
        events = [{"id": "t1", "title": "Morning walk", "start": at(8, 0, d1).isoformat(),
                   "end": at(8, 30, d1).isoformat()},
                  {"id": "t2", "title": "Solidcore", "start": at(9, 0, d1).isoformat(),
                   "end": at(9, 50, d1).isoformat()}]
        sent = []
        kw = dict(config=CFG, db_path=db_path, events=events, sync_fn=lambda recent: {"ok": True},
                  send=lambda t, b, p: sent.append((t, b)) or True)
        self.assertEqual(watcher.run_once(now=at(19, 50), **kw), [])  # before 20:00
        out = watcher.run_once(now=at(20, 2), **kw)
        self.assertEqual([o["kind"] for o in out], ["preview"])
        self.assertEqual(out[0]["event_id"], "preview@%s" % d1.isoformat())
        title, body = sent[0]
        self.assertIn("tomorrow", title)
        self.assertIn("Walk 8:00 AM", body)
        self.assertIn("Solidcore 9:00 AM", body)
        self.assertIn("re-check 1 hour", body)
        self.assertNotIn("pilates", body.lower())
        self.assertEqual(watcher.run_once(now=at(21), **kw), [])  # deduped: one per date
        self.assertEqual(len(sent), 1)
        # late-cancel deadline: preview comes earlier, and a past deadline is mentioned kindly
        db2 = os.path.join(self.tmp, "watch4.db")
        shutil.copy(self.low_db, db2)
        cfg = dict(CFG, late_cancel_hours={"pilates": 24, "walking": None})
        sent2 = []
        out = watcher.run_once(now=at(15), config=cfg, db_path=db2, events=events,
                               sync_fn=lambda recent: {"ok": True},
                               send=lambda t, b, p: sent2.append(b) or True)
        self.assertEqual([o["kind"] for o in out], ["preview"])
        if out[0]["verdict"] != "green":
            self.assertIn("late-cancel window has passed", sent2[0])
        # disabled preview -> nothing
        cfg_off = dict(CFG, preview={"enabled": False, "time": "20:00"})
        db3 = os.path.join(self.tmp, "watch5.db")
        shutil.copy(self.low_db, db3)
        self.assertEqual(watcher.run_once(now=at(20, 30), config=cfg_off, db_path=db3, events=events,
                                          sync_fn=lambda recent: {"ok": True}, send=lambda *a: True), [])

    def test_morning_digest(self):
        db_path = os.path.join(self.tmp, "watch_m.db")
        shutil.copy(self.low_db, db_path)
        d1 = fixture.TODAY + timedelta(days=1)
        events = [ev("BA780 class", 9, 30, 75, eid="c1"), ev("Padel", 19, 0, 60, eid="p1"),
                  {"id": "w2", "title": "Morning walk", "start": at(8, 0, d1).isoformat(),
                   "end": at(8, 30, d1).isoformat()}]
        sent = []
        kw = dict(config=CFG, db_path=db_path, events=events, sync_fn=lambda recent: {"ok": True},
                  send=lambda t, b, p: sent.append((t, b)) or True)
        self.assertEqual(watcher.run_once(now=at(6, 50), **kw), [])  # before 07:00
        out = watcher.run_once(now=at(7, 5), **kw)
        self.assertEqual([o["kind"] for o in out], ["morning"])
        self.assertEqual(out[0]["event_id"], "morning@%s" % fixture.TODAY.isoformat())
        title, body = sent[0]
        self.assertIn("next 48 hours", title)
        self.assertTrue(body.startswith("Today: "))
        self.assertIn("Class 9:30 am", body)
        self.assertIn("Padel 7:00 pm", body)
        self.assertIn("Tomorrow: ", body)
        self.assertEqual(watcher.run_once(now=at(7, 20), **kw), [])  # deduped
        # daily-kind events never get a 1-hour precheck
        db2 = os.path.join(self.tmp, "watch_m2.db")
        shutil.copy(self.low_db, db2)
        cfg_off = dict(CFG, morning={"enabled": False, "time": "07:00"})
        out = watcher.run_once(now=at(8, 45), config=cfg_off, db_path=db2, events=events,
                               sync_fn=lambda recent: {"ok": True}, send=lambda *a: True)
        self.assertEqual(out, [])

    def test_osascript_escape(self):
        self.assertEqual(watcher._osa_escape('say "hi" \\ ok'), 'say \\"hi\\" \\\\ ok')
        self.assertTrue(watcher._header_safe("PlanWell — walk").startswith("=?UTF-8?B?"))


if __name__ == "__main__":
    unittest.main()
