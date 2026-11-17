"""Analytics tests (stdlib unittest): backtest is leak-free, shapes match the SPEC addendum,
insights are skipped when groups are small. Run: python3 -m unittest discover -s tests -v"""
import copy
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fixture  # noqa: E402
from planwell import analytics, engine, mock_data  # noqa: E402
from planwell.settings import DEFAULT_CONFIG  # noqa: E402

TZ = fixture.TZ
CFG = copy.deepcopy(DEFAULT_CONFIG)
NOW = datetime(fixture.TODAY.year, fixture.TODAY.month, fixture.TODAY.day, 14, 0, tzinfo=TZ)

CONF_KEYS = {"tp", "fp", "fn", "tn", "precision", "recall", "flag_rate"}
INSIGHT_IDS = {"sleep", "hrv", "readiness", "time_of_day", "activity_compare", "recovery_trend"}


def add_future_sessions(path, n_days=6):
    """Append walks + pilates on the days after fixture.TODAY (with HR), like new data arriving."""
    conn = sqlite3.connect(str(path))
    hr = []
    for i in range(1, n_days + 1):
        day = fixture.TODAY + timedelta(days=i)
        for act, h, dur, base in (("walking", 9, 30, 150), ("pilates", 18, 50, 155)):
            st = fixture.local(day, h)
            en = st + timedelta(minutes=dur)
            wid = "future-%s-%d" % (act, i)
            conn.execute("INSERT INTO workouts VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (wid, act, day.isoformat(), st.isoformat(), en.isoformat(), "hard", 1, 1,
                          "manual", None, json.dumps({"id": wid})))
            for k in range(dur * 2):
                hr.append((fixture.utc(st + timedelta(seconds=30 * k)), base, "workout"))
        conn.execute("INSERT OR REPLACE INTO daily_readiness VALUES (?,?,?,?,?,?)",
                     (day.isoformat(), 40, 1.0, 10, 10, "{}"))
    conn.executemany("INSERT OR REPLACE INTO heartrate VALUES (?,?,?)", hr)
    conn.commit()
    conn.close()


class AnalyticsBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="pw-analytics-")

    @classmethod
    def tearDownClass(cls):
        engine.clear_cache()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def db(self, name, **kw):
        p = os.path.join(self.tmp, name)
        if os.path.exists(p):
            os.unlink(p)
        fixture.build(p, **kw)
        return p


class TestBacktest(AnalyticsBase):
    def test_shape(self):
        res = analytics.backtest(self.db("shape.db"), CFG, now=NOW)
        for k in ("generated_at", "n_sessions", "method", "hard_definition", "planwell", "baseline",
                  "by_verdict", "timeline"):
            self.assertIn(k, res)
        self.assertEqual(res["method"], "Each session judged only with data from before it.")
        self.assertTrue(CONF_KEYS <= set(res["planwell"]))
        self.assertTrue(CONF_KEYS | {"name"} <= set(res["baseline"]))
        self.assertEqual([v["verdict"] for v in res["by_verdict"]], ["green", "yellow", "red"])
        self.assertGreater(res["n_sessions"], 10)
        self.assertEqual(res["n_sessions"], len(res["timeline"]))
        pw = res["planwell"]
        self.assertEqual(pw["tp"] + pw["fp"] + pw["fn"] + pw["tn"], res["n_sessions"])
        self.assertEqual(sum(v["n"] for v in res["by_verdict"]), res["n_sessions"])
        for row in res["timeline"]:
            self.assertEqual(set(row), {"workout_id", "date", "activity", "verdict", "minutes_per_30", "hard"})
            self.assertIn(row["verdict"], ("green", "yellow", "red"))
            self.assertIsInstance(row["hard"], bool)
        dates = [r["date"] for r in res["timeline"]]
        self.assertEqual(dates, sorted(dates))
        self.assertIn("bpm per 30 min", res["hard_definition"])
        json.dumps(res)

    def test_min_history_skips_early_sessions(self):
        p = self.db("minhist.db")
        few = analytics.backtest(p, CFG, min_history=8, now=NOW)
        many = analytics.backtest(p, CFG, min_history=20, now=NOW)
        self.assertLess(many["n_sessions"], few["n_sessions"])
        # pilates has ~13 sessions in the fixture -> none qualify with 20 earlier ones required
        self.assertFalse([r for r in many["timeline"] if r["activity"] == "pilates"])

    def test_leak_free_future_sessions_dont_change_verdicts(self):
        p1 = self.db("past.db")
        before = analytics.backtest(p1, CFG, now=NOW)
        p2 = os.path.join(self.tmp, "past_plus_future.db")
        shutil.copy(p1, p2)
        add_future_sessions(p2)
        later = NOW + timedelta(days=10)
        after = analytics.backtest(p2, CFG, now=later)
        v1 = {r["workout_id"]: r["verdict"] for r in before["timeline"]}
        v2 = {r["workout_id"]: r["verdict"] for r in after["timeline"]}
        self.assertTrue(v1)
        self.assertGreater(len(v2), len(v1))  # the future sessions were judged too
        for wid, verdict in v1.items():
            self.assertEqual(v2[wid], verdict, "verdict for %s changed when later data was added" % wid)

    def test_replay_only_uses_earlier_sessions(self):
        p = self.db("replay.db")
        store = engine.get_store(p)
        recs = [r for r in engine._session_records(store, CFG) if r["rate"] is not None]
        recs.sort(key=lambda r: r["w"]["start"])
        target = recs[-1]
        c = analytics.replay_check(store, CFG, target)
        self.assertTrue(c["similar_days"])
        for s in c["similar_days"]:
            self.assertNotEqual(s["workout_id"], target["w"]["id"])
            self.assertLess(s["date"], target["w"]["day"] + "T")  # same-day earlier sessions are allowed
            w = store.by_id[s["workout_id"]]
            self.assertLessEqual(w["end"], target["w"]["start"] - timedelta(minutes=60))

    def test_detects_the_fixture_pattern(self):
        # fixture: every 4th day is a low day and sessions on low days run much hotter
        res = analytics.backtest(self.db("pattern.db"), CFG, now=NOW)
        self.assertGreaterEqual(res["planwell"]["recall"], 0.5)
        green = [v for v in res["by_verdict"] if v["verdict"] == "green"][0]
        flagged = [r for r in res["timeline"] if r["verdict"] != "green"]
        self.assertTrue(flagged)
        if green["n"]:
            avg_flagged = sum(r["minutes_per_30"] for r in flagged) / len(flagged)
            self.assertGreater(avg_flagged, green["avg_minutes"])

    def test_empty_db(self):
        p = self.db("empty.db", workouts=False)
        res = analytics.backtest(p, CFG, now=NOW)
        self.assertEqual(res["n_sessions"], 0)
        self.assertEqual(res["timeline"], [])
        self.assertEqual(res["planwell"]["precision"], 0)


class TestInsights(AnalyticsBase):
    def check_shape(self, res):
        for k in ("generated_at", "n_sessions", "metric", "insights", "caveat"):
            self.assertIn(k, res)
        self.assertEqual(res["metric"], "minutes above 125 bpm per 30 min")
        self.assertIn("not medical advice", res["caveat"])
        ids = [i["id"] for i in res["insights"]]
        self.assertEqual(len(ids), len(set(ids)))
        for i in res["insights"]:
            self.assertIn(i["id"], INSIGHT_IDS)
            self.assertIn(i["activity"], ("pilates", "walking", "all"))
            self.assertIn(i["confidence"], ("low", "medium", "high"))
            for g in ("a", "b"):
                self.assertEqual({"label", "value", "n"} <= set(i[g]), True)
                self.assertGreaterEqual(i[g]["n"], 4)
            n = min(i["a"]["n"], i["b"]["n"])
            self.assertEqual(i["confidence"], "low" if n < 6 else "medium" if n < 15 else "high")
            self.assertTrue(i["finding"].endswith("."))
            self.assertNotIn("pilates", i["finding"].lower())  # always "Solidcore" in user-facing text
        json.dumps(res)

    def test_shape_on_fixture(self):
        res = analytics.insights(self.db("ins.db"), CFG, now=NOW)
        self.check_shape(res)
        self.assertEqual(set(res["n_sessions"]), set(analytics._registry(CFG)))
        self.assertTrue({"walking", "pilates"} <= set(res["n_sessions"]))
        self.assertTrue(res["insights"])

    def test_small_groups_are_skipped(self):
        res = analytics.insights(self.db("tiny.db", max_workouts=6), CFG, now=NOW)
        self.check_shape(res)
        # at most 6 sessions in total: no comparison can have two groups of >= 4
        self.assertEqual(res["insights"], [])

    def test_no_data(self):
        res = analytics.insights(self.db("none.db", workouts=False), CFG, now=NOW)
        self.check_shape(res)
        self.assertEqual(res["insights"], [])
        self.assertEqual(set(res["n_sessions"].values()), {0})

    def test_mock_data_findings_use_solidcore_label(self):
        p = os.path.join(self.tmp, "mock.db")
        now = datetime(2026, 11, 4, 15, 30, tzinfo=TZ)
        mock_data.generate(days=120, seed=7, today_preset="meh", db_path=p,
                           cal_path=os.path.join(self.tmp, "cal.json"), now=now, demo_calendar=True)
        res = analytics.insights(p, CFG, now=now)
        self.check_shape(res)
        self.assertGreaterEqual(len(res["insights"]), 4)
        text = " ".join(i["finding"] for i in res["insights"])
        self.assertIn("Solidcore", text)
        bt = analytics.backtest(p, CFG, now=now)
        self.assertGreater(bt["n_sessions"], 50)
        # every registry activity is reported; padel has only ~4 sessions -> skipped at min_history=8
        self.assertTrue({"walking", "pilates", "padel", "class"} <= set(bt["by_activity"]))
        self.assertEqual(bt["by_activity"]["padel"]["n"], 0)
        self.assertGreater(bt["by_activity"]["class"]["n"], 10)
        acts = {r["activity"] for r in analytics.backtest(p, CFG, min_history=2, now=now)["timeline"]}
        self.assertIn("padel", acts)
        self.assertEqual(res["n_sessions"]["padel"], 4)
        self.assertGreater(res["n_sessions"]["class"], 30)

    def test_phrases(self):
        self.assertEqual(analytics._ratio_phrase(9.2, 3.1)[0], "about 2.5× harder")
        self.assertEqual(analytics._ratio_phrase(5.0, 5.2)[0], "about the same")
        self.assertEqual(analytics._ratio_phrase(2.0, 6.0)[0], "noticeably calmer")
        self.assertEqual(analytics._ratio_phrase(30.0, 2.0)[0], "more than 4× harder")
        self.assertEqual(analytics._confidence(5, 40), "low")
        self.assertEqual(analytics._confidence(14, 40), "medium")
        self.assertEqual(analytics._confidence(15, 40), "high")


class TestMockDemoCalendar(unittest.TestCase):
    def test_demo_calendar_fixed_times(self):
        tmp = tempfile.mkdtemp(prefix="pw-cal-")
        try:
            now = datetime(2026, 11, 4, 15, 30, tzinfo=TZ)
            cal = os.path.join(tmp, "cal.json")
            ev = mock_data.write_demo_calendar(now, __import__("pathlib").Path(cal))
            from planwell.classify import classify
            today = [(e["start"][11:16], classify(e, CFG)) for e in ev if e["start"].startswith("2026-11-04")]
            tom = [(e["start"][11:16], classify(e, CFG)) for e in ev if e["start"].startswith("2026-11-05")]
            self.assertIn(("09:30", "class"), today)
            self.assertIn(("17:00", "pilates"), today)
            self.assertIn(("19:00", "walking"), today)
            self.assertIn("walking", [a for hm, a in tom if hm < "12:00"])
            self.assertIn(("13:00", "class"), tom)
            self.assertIn(("19:00", "padel"), tom)
            self.assertIn(None, [a for _hm, a in today])  # non-activity events too
            from planwell import labeler
            hot = [e for e in ev if e["title"].startswith("Hot yoga")]
            self.assertEqual(len(hot), 1)
            self.assertTrue(labeler.is_unrecognised(labeler.label_event(hot[0], CFG)))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
