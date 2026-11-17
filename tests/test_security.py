"""Security guards for the local server and notifications."""
import http.client
import json
import os
import shutil
import stat
import tempfile
import threading
import unittest
from pathlib import Path

from planwell import db, server, watcher, settings


class ServerGuards(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cfg = cls.tmp / "config.json"
        cfg.write_text("{}")
        cls.app = server.App(mock=True, config_path=cfg, db_path=cls.tmp / "t.db")
        cls.httpd = server.make_server(cls.app, 0)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def req(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Host": "127.0.0.1:%d" % self.port}
        h.update(headers or {})
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        data = r.read()
        hdrs = dict(r.getheaders())
        c.close()
        return r.status, data, hdrs

    def test_dns_rebinding_host_rejected(self):
        st, _, _ = self.req("GET", "/api/settings", headers={"Host": "evil.example:%d" % self.port})
        self.assertEqual(st, 403)

    def test_cross_site_origin_rejected(self):
        st, _, _ = self.req("POST", "/api/settings", body=json.dumps({"caution_style": "flexible"}),
                            headers={"Origin": "https://evil.example", "Content-Type": "application/json"})
        self.assertEqual(st, 403)

    def test_simple_form_post_rejected(self):
        st, _, _ = self.req("POST", "/api/settings", body='{"caution_style":"flexible"}',
                            headers={"Content-Type": "text/plain"})
        self.assertEqual(st, 415)

    def test_same_origin_json_ok_and_headers(self):
        st, _, h = self.req("GET", "/api/settings", headers={"Origin": "http://127.0.0.1:%d" % self.port})
        self.assertEqual(st, 200)
        self.assertEqual(h.get("X-Frame-Options"), "DENY")
        self.assertIn("frame-ancestors 'none'", h.get("Content-Security-Policy", ""))

    def test_path_traversal_blocked(self):
        st, _, _ = self.req("GET", "/../secrets/oura_tokens.json")
        self.assertIn(st, (403, 404))
        st, _, _ = self.req("GET", "/%2e%2e/config.json")
        self.assertIn(st, (403, 404))


class Privacy(unittest.TestCase):
    def test_db_files_private(self):
        d = Path(tempfile.mkdtemp())
        try:
            p = d / "data" / "x.db"
            db.connect(p).close()
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(os.stat(p.parent).st_mode), 0o700)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_ntfy_minimal_by_default(self):
        sent = []
        orig = watcher.notify_ntfy
        watcher.notify_ntfy = lambda topic, t, b, v, server="": sent.append((t, b, v)) or True
        try:
            cfg = {"notify": {"macos": False, "ntfy_topic": "abcdefghijklmnop1234"}}
            watcher._deliver("PlanWell: Rest may help - Padel 7pm", "HRV is 24% below your usual", "red",
                             {}, cfg, False, None)
        finally:
            watcher.notify_ntfy = orig
        self.assertEqual(len(sent), 1)
        for t, b, v in sent:
            self.assertNotIn("HRV", b)
            self.assertNotIn("Padel", t)

    def test_ntfy_settings_validation(self):
        with self.assertRaises(ValueError):
            settings.validate_partial({"notify": {"ntfy_topic": "short"}})
        with self.assertRaises(ValueError):
            settings.validate_partial({"notify": {"ntfy_server": "http://insecure.example"}})


if __name__ == "__main__":
    unittest.main()


class Classification(unittest.TestCase):
    def test_starts_with_rule_and_camelcase_oura_types(self):
        from planwell import classify, engine
        path = Path(tempfile.mkdtemp()) / "c.json"
        cur = settings.load_config(path)
        settings.update_config({"activities": {
            "class": {"keywords": cur["activities"]["class"]["keywords"] + ["^BA"]},
            "gym": {"label": "Gym", "kind": "exercise", "keywords": ["gym"],
                    "oura_types": ["strengthTraining", "coreExercise"],
                    "traits": {"posture": "mixed", "pattern": "bursty", "setting": "indoor"},
                    "late_cancel_hours": None}}}, path)
        cfg = settings.load_config(path)
        ev = lambda t: {"title": t, "start": "2026-11-12T10:00:00-05:00", "end": "2026-11-12T11:00:00-05:00"}
        self.assertEqual(classify.classify(ev("BA780"), cfg), "class")
        self.assertEqual(classify.classify(ev("BA 712"), cfg), "class")
        self.assertNotEqual(classify.classify(ev("Bake sale"), cfg), "class")
        self.assertNotEqual(classify.classify(ev("Barre"), cfg), "class")
        self.assertEqual(engine.norm_activity("strengthTraining", cfg), "gym")
        self.assertEqual(engine.norm_activity("coreExercise", cfg), "gym")
