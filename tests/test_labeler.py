"""Labeler tests: strict payload whitelist, rules labeller, providers via a FAKE http layer (no network)."""
import copy
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from planwell import labeler  # noqa: E402
from planwell.settings import DEFAULT_CONFIG  # noqa: E402

CFG = copy.deepcopy(DEFAULT_CONFIG)
CFG["timezone"] = "America/New_York"

EVENT = {
    "id": "ev1", "title": "Hot yoga w/ Deniz", "location": "Inferno Studio, Back Bay",
    "start": "2026-11-05T18:00:00-05:00", "end": "2026-11-05T19:15:00-05:00",
    # everything below must NEVER reach a labeller
    "description": "secret notes", "attendees": [{"email": "deniz@example.com"}], "calendar_id": "primary",
    "verdict": "red", "readiness": 52, "hrv": 31, "activity": "pilates", "organizer": "me@example.com",
}
SECRET_BITS = ["secret notes", "deniz@example.com", "primary", "readiness", "hrv", "verdict", "52", "31",
               "me@example.com", "description", "attendees"]


def cfg_with(provider, **extra):
    c = copy.deepcopy(CFG)
    c["labeler"] = dict({"provider": provider}, **extra)
    return c


class FakeHTTP(object):
    def __init__(self, status=200, text=None, raw=None, exc=None):
        self.calls = []
        self.status, self.text, self.raw, self.exc = status, text, raw, exc

    def __call__(self, url, headers, body, timeout):
        self.calls.append({"url": url, "headers": headers, "body": body, "timeout": timeout})
        if self.exc:
            raise self.exc
        if self.raw is not None:
            return self.status, self.raw
        return self.status, json.dumps({"content": [{"type": "text", "text": self.text}]}).encode("utf-8")


GOOD = json.dumps({"category": "other", "posture": "mixed", "pattern": "steady", "setting": "indoor", "exertion": 4})


class TestPayload(unittest.TestCase):
    def test_payload_keys_are_exactly_the_whitelist(self):
        p = labeler.build_payload(EVENT, CFG)
        self.assertEqual(set(p), {"title", "location", "duration_min", "start_hour", "weekday"})
        self.assertEqual(tuple(sorted(p)), tuple(sorted(labeler.PAYLOAD_KEYS)))
        self.assertEqual(p["duration_min"], 75)
        self.assertEqual(p["start_hour"], 18)
        self.assertEqual(p["weekday"], "Thursday")
        self.assertEqual(p["title"], "Hot yoga w/ Deniz")

    def test_payload_handles_google_shape_and_missing(self):
        p = labeler.build_payload({"summary": "Walk", "start": {"dateTime": "2026-11-05T07:30:00-05:00"},
                                   "end": {"dateTime": "2026-11-05T08:00:00-05:00"}}, CFG)
        self.assertEqual((p["title"], p["duration_min"], p["start_hour"]), ("Walk", 30, 7))
        p = labeler.build_payload({}, CFG)
        self.assertEqual(set(p), set(labeler.PAYLOAD_KEYS))
        self.assertIsNone(p["duration_min"])

    def test_payload_text_is_trimmed(self):
        p = labeler.build_payload({"title": "x" * 1000, "location": "a\n\n  b"}, CFG)
        self.assertEqual(len(p["title"]), labeler.MAX_TEXT)
        self.assertEqual(p["location"], "a b")


class TestRules(unittest.TestCase):
    def lab(self, title, location="", start="2026-11-05T18:00:00-05:00", end="2026-11-05T19:00:00-05:00"):
        return labeler.label_event({"title": title, "location": location, "start": start, "end": end}, CFG)

    def test_known_activities(self):
        self.assertEqual(self.lab("[solidcore] Arms + Abs")["category"], "pilates")
        self.assertEqual(self.lab("Solidcore — Full Body")["category"], "pilates")
        self.assertEqual(self.lab("Evening walk")["category"], "walking")
        self.assertEqual(self.lab("Padel w/ Ana", "Court 3")["category"], "padel")
        self.assertEqual(self.lab("BA780 class", "Hall B")["category"], "class")
        self.assertEqual(self.lab("MSBA seminar")["category"], "class")
        self.assertEqual(self.lab("Something", "[solidcore] Back Bay")["category"], "pilates")
        c = self.lab("BA780 class")
        self.assertEqual((c["posture"], c["pattern"], c["source"]), ("seated", "still", "rules"))
        p = self.lab("Padel w/ Ana")
        self.assertEqual(p["pattern"], "bursty")
        self.assertGreaterEqual(p["exertion"], 3)

    def test_unrecognised_exertion(self):
        hy = self.lab("Hot yoga w/ Deniz", "Inferno Studio")
        self.assertEqual(hy["category"], "other")
        self.assertGreaterEqual(hy["exertion"], 4)
        self.assertEqual(hy["setting"], "indoor")
        self.assertTrue(labeler.is_unrecognised(hy))
        self.assertIsNotNone(labeler.suggest_activity(hy, CFG))
        for t in ("Spin class", "Tennis with Sam", "Bouldering", "Help move boxes", "Running club"):
            # "Spin class" contains "class" -> tracked as class keyword first; others are unrecognised
            lab = self.lab(t)
            if lab["category"] == "other":
                self.assertGreaterEqual(lab["exertion"], 3, t)
        for t in ("Coffee chat", "Team check-in call", "Dentist", "Call with Mom", "Dinner"):
            lab = self.lab(t)
            self.assertEqual(lab["category"], "other", t)
            self.assertLessEqual(lab["exertion"], 2, t)
            self.assertFalse(labeler.is_unrecognised(lab), t)

    def test_always_valid(self):
        for t in ("", "???", "x", "Hot hot hot", "Lunch at the park", "Moving day"):
            lab = self.lab(t)
            self.assertIsNotNone(labeler.validate_label(lab, CFG), t)
            self.assertEqual(lab["source"], "rules")

    def test_registry_from_v3_config(self):
        c = copy.deepcopy(CFG)
        c["activities"] = {"swim": {"label": "Swim", "kind": "exercise", "keywords": ["swim", "pool"],
                                    "oura_types": ["swimming"],
                                    "traits": {"posture": "mixed", "pattern": "steady", "setting": "indoor"}}}
        lab = labeler.label_event({"title": "Pool laps"}, c)
        self.assertEqual(lab["category"], "swim")
        self.assertEqual(set(labeler.registry(c)), {"swim"})


class TestProviders(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pw-lab-")
        self.db = os.path.join(self.tmp, "t.db")
        self.env = mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"})
        self.env.start()
        self.nokey = mock.patch.object(labeler, "ANTHROPIC_KEY_FILE", Path(self.tmp) / "missing.txt")
        self.nokey.start()

    def tearDown(self):
        self.env.stop()
        self.nokey.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def assert_body_is_clean(self, body_bytes):
        text = body_bytes.decode("utf-8")
        for bit in SECRET_BITS:
            self.assertNotIn(bit, text)
        body = json.loads(text)
        user = body["messages"][0]["content"]
        ev = json.loads(user.split("Event: ", 1)[1].split("\n", 1)[0])
        self.assertEqual(set(ev), set(labeler.PAYLOAD_KEYS))
        return body

    def test_anthropic_request_and_parse(self):
        http = FakeHTTP(text=GOOD)
        lab = labeler.label_event(EVENT, cfg_with("anthropic"), db_path=self.db, http=http)
        self.assertEqual(lab["source"], "ai")
        self.assertEqual(lab["exertion"], 4)
        call = http.calls[0]
        self.assertEqual(call["url"], "https://api.anthropic.com/v1/messages")
        self.assertEqual(call["headers"]["x-api-key"], "test-key")
        self.assertEqual(call["headers"]["anthropic-version"], "2023-06-01")
        body = self.assert_body_is_clean(call["body"])
        self.assertEqual(body["model"], "claude-haiku-5-5")
        self.assertNotIn("anthropic_version", body)

    def test_cache_hit_skips_network(self):
        http = FakeHTTP(text=GOOD)
        c = cfg_with("anthropic")
        labeler.label_event(EVENT, c, db_path=self.db, http=http)
        again = labeler.label_event(dict(EVENT, description="changed notes"), c, db_path=self.db, http=http)
        self.assertEqual(again["source"], "cache")
        self.assertEqual(len(http.calls), 1)
        conn = sqlite3.connect(self.db)
        rows = conn.execute("SELECT key, label_json FROM event_labels").fetchall()
        conn.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], labeler.payload_key(labeler.build_payload(EVENT, c)))
        self.assertEqual(len(rows[0][0]), 64)

    def test_vertex_request(self):
        http = FakeHTTP(text="```json\n%s\n```" % GOOD)
        lab = labeler.label_event(EVENT, cfg_with("vertex"), db_path=self.db, http=http,
                                  token_fn=lambda: "tok123")
        self.assertEqual(lab["source"], "ai")
        call = http.calls[0]
        self.assertEqual(call["url"], "https://us-east5-aiplatform.googleapis.com/v1/projects/your-gcp-project/"
                                      "locations/us-east5/publishers/anthropic/models/claude-haiku-5-5:rawPredict")
        self.assertEqual(call["headers"]["Authorization"], "Bearer tok123")
        body = self.assert_body_is_clean(call["body"])
        self.assertEqual(body["anthropic_version"], "vertex-2023-10-16")
        self.assertNotIn("model", body)

    def test_vertex_uses_gcloud_subprocess(self):
        fake = mock.Mock(returncode=0, stdout=b"gtok\n", stderr=b"")
        with mock.patch.object(labeler.subprocess, "run", return_value=fake) as run:
            http = FakeHTTP(text=GOOD)
            labeler.label_event(EVENT, cfg_with("vertex"), http=http)
        self.assertEqual(run.call_args[0][0], ["gcloud", "auth", "print-access-token"])
        self.assertEqual(http.calls[0]["headers"]["Authorization"], "Bearer gtok")

    def test_fallbacks_to_rules(self):
        bad = [FakeHTTP(status=500, text=GOOD), FakeHTTP(text="not json"),
               FakeHTTP(text=json.dumps({"category": "nope", "posture": "mixed", "pattern": "steady",
                                         "setting": "indoor", "exertion": 4})),
               FakeHTTP(text=json.dumps({"category": "other", "posture": "mixed", "pattern": "steady",
                                         "setting": "indoor", "exertion": 9})),
               FakeHTTP(raw=b"<html>"), FakeHTTP(exc=OSError("network down"))]
        for http in bad:
            lab = labeler.label_event(EVENT, cfg_with("anthropic"), db_path=self.db, http=http)
            self.assertEqual(lab["source"], "rules")
            self.assertEqual(len(http.calls), 1)
        # nothing invalid was cached
        conn = sqlite3.connect(self.db)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM event_labels").fetchone()[0], 0)
        conn.close()

    def test_no_credentials_means_no_request(self):
        http = FakeHTTP(text=GOOD)
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
            lab = labeler.label_event(EVENT, cfg_with("anthropic"), http=http)
        self.assertEqual(lab["source"], "rules")
        lab = labeler.label_event(EVENT, cfg_with("vertex"), http=http, token_fn=lambda: None)
        self.assertEqual(lab["source"], "rules")
        self.assertEqual(http.calls, [])

    def test_provider_none_never_calls_http(self):
        http = FakeHTTP(text=GOOD)
        self.assertEqual(labeler.label_event(EVENT, cfg_with("none"), http=http)["source"], "rules")
        self.assertEqual(labeler.label_event(EVENT, CFG, http=http)["source"], "rules")
        self.assertEqual(http.calls, [])

    def test_label_events_dedupes(self):
        http = FakeHTTP(text=GOOD)
        evs = [dict(EVENT, id="a"), dict(EVENT, id="b"), {"id": "c", "title": "Evening walk"}]
        out = labeler.label_events(evs, cfg_with("anthropic"), http=http)
        self.assertEqual(set(out), {"a", "b", "c"})
        self.assertEqual(len(http.calls), 2)


if __name__ == "__main__":
    unittest.main()
