"""Offline tests for oura_client + sync using a fake HTTP layer (no network, no real data).

    python3 -m unittest tests.test_oura_client -v
"""
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
from datetime import date
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from planwell import db, oura_client, sync  # noqa: E402
from planwell.oura_client import OuraAPIError, OuraAuthError, OuraClient  # noqa: E402

NOW = 1_800_000_000.0


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def http_error(url, code, headers=None):
    import email.message
    msg = email.message.Message()
    for k, v in (headers or {}).items():
        msg[k] = v
    return urllib.error.HTTPError(url, code, "err", msg, io.BytesIO(b"{}"))


class FakeOura(object):
    """Scriptable urlopen replacement. `script` is a list of callables(req)->obj|Exception."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def __call__(self, req, timeout=None):
        self.calls.append(req)
        out = self.script.pop(0)(req)
        if isinstance(out, Exception):
            raise out
        return FakeResp(json.dumps(out).encode())


def token_resp(n):
    return lambda req: {"access_token": "acc%d" % n, "refresh_token": "ref%d" % n,
                        "expires_in": 86400, "token_type": "bearer"}


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        (self.dir / "oura_app.json").write_text(json.dumps({"client_id": "cid", "client_secret": "sec"}))
        self.write_tokens(obtained_at=NOW - 100)
        self.sleeps = []

    def write_tokens(self, obtained_at):
        (self.dir / "oura_tokens.json").write_text(json.dumps({
            "access_token": "acc0", "refresh_token": "ref0", "expires_in": 86400,
            "token_type": "bearer", "obtained_at": int(obtained_at)}))

    def client(self, fake):
        return OuraClient(secrets_dir=self.dir, urlopen=fake, sleep=self.sleeps.append, now=lambda: NOW)

    def tokens(self):
        return json.loads((self.dir / "oura_tokens.json").read_text())

    def test_missing_tokens(self):
        (self.dir / "oura_tokens.json").unlink()
        with self.assertRaises(OuraAuthError) as cm:
            self.client(FakeOura([]))
        self.assertIn("oura_connect.py", str(cm.exception))

    def test_pagination(self):
        fake = FakeOura([lambda r: {"data": [{"id": 1}], "next_token": "T2"},
                         lambda r: {"data": [{"id": 2}], "next_token": None}])
        rows = self.client(fake).get_all("workout", start_date="2026-01-01", end_date="2026-01-02")
        self.assertEqual([r["id"] for r in rows], [1, 2])
        q2 = urllib.parse.parse_qs(urllib.parse.urlparse(fake.calls[1].full_url).query)
        self.assertEqual(q2["next_token"], ["T2"])
        self.assertEqual(fake.calls[0].get_header("Authorization"), "Bearer acc0")

    def test_refresh_when_near_expiry_persists_atomically(self):
        self.write_tokens(obtained_at=NOW - 86400 + 1800)  # 30 min left
        fake = FakeOura([token_resp(1), lambda r: {"data": []}])
        self.client(fake).get_all("daily_sleep")
        body = urllib.parse.parse_qs(fake.calls[0].data.decode())
        self.assertEqual(body["grant_type"], ["refresh_token"])
        self.assertEqual(body["refresh_token"], ["ref0"])
        t = self.tokens()
        self.assertEqual((t["access_token"], t["refresh_token"], t["obtained_at"]), ("acc1", "ref1", int(NOW)))
        self.assertEqual(fake.calls[1].get_header("Authorization"), "Bearer acc1")
        self.assertEqual([p.name for p in self.dir.iterdir() if p.name.startswith(".tmp")], [])

    def test_401_triggers_single_refresh_then_retry(self):
        fake = FakeOura([lambda r: http_error(r.full_url, 401), token_resp(1),
                         lambda r: {"data": [{"x": 1}]}])
        self.assertEqual(len(self.client(fake).get_all("sleep")), 1)
        self.assertEqual(self.tokens()["refresh_token"], "ref1")

    def test_repeated_401_gives_reconnect_hint(self):
        fake = FakeOura([lambda r: http_error(r.full_url, 401), token_resp(1),
                         lambda r: http_error(r.full_url, 401)])
        with self.assertRaises(OuraAuthError) as cm:
            self.client(fake).get("sleep")
        self.assertIn("oura_connect.py", str(cm.exception))

    def test_refresh_rejected(self):
        self.write_tokens(obtained_at=0)
        fake = FakeOura([lambda r: http_error(r.full_url, 400)])
        with self.assertRaises(OuraAuthError):
            self.client(fake).get("sleep")
        self.assertEqual(self.tokens()["refresh_token"], "ref0")  # untouched

    def test_429_retry_after(self):
        fake = FakeOura([lambda r: http_error(r.full_url, 429, {"Retry-After": "7"}),
                         lambda r: http_error(r.full_url, 429),
                         lambda r: {"data": []}])
        self.client(fake).get("heartrate")
        self.assertEqual(self.sleeps[0], 7.0)
        self.assertEqual(len(self.sleeps), 2)

    def test_read_timeout_is_retried(self):
        import socket
        fake = FakeOura([lambda r: socket.timeout("The read operation timed out"),
                         lambda r: TimeoutError("The read operation timed out"),
                         lambda r: {"data": [{"ok": 1}]}])
        self.assertEqual(self.client(fake).get("heartrate")["data"], [{"ok": 1}])
        self.assertEqual(len(self.sleeps), 2)

    def test_persistent_timeout_becomes_api_error(self):
        import socket
        fake = FakeOura([lambda r: socket.timeout("timed out")] * 10)
        with self.assertRaises(OuraAPIError):
            self.client(fake).get("heartrate")


class FakeClient(object):
    """Stands in for OuraClient inside sync()."""

    def __init__(self):
        self.calls = []

    def get_all(self, path, **params):
        self.calls.append((path, params))
        if path == "workout":
            return [{"id": "w1", "activity": "walking", "day": "2026-11-09",
                     "start_datetime": "2026-11-09T08:00:00-05:00",
                     "end_datetime": "2026-11-09T08:30:00-05:00", "intensity": "easy",
                     "calories": 120.0, "distance": 2400.0, "source": "manual", "label": None}]
        if path == "daily_readiness":
            return [{"day": "2026-11-09", "score": 70, "temperature_deviation": 0.1,
                     "contributors": {"resting_heart_rate": 80, "hrv_balance": 66}}]
        if path == "heartrate":
            return [{"timestamp": "2026-11-09T13:00:00.000Z", "bpm": 101, "source": "workout"}]
        return []


class SyncTests(unittest.TestCase):
    def test_sync_maps_and_chunks(self):
        path = Path(tempfile.mkdtemp()) / "t.db"
        fc = FakeClient()
        res = sync.sync(days=65, db_path=path, client=fc, today=date(2026, 11, 10))
        self.assertTrue(res["ok"], res)
        hr_calls = [c for c in fc.calls if c[0] == "heartrate"]
        self.assertGreaterEqual(len(hr_calls), 3)  # 65+ days in <=30-day chunks
        conn = db.connect(path)
        r = conn.execute("SELECT * FROM daily_readiness").fetchone()
        self.assertEqual((r["resting_heart_rate_contrib"], r["hrv_balance_contrib"]), (80, 66))
        self.assertEqual(conn.execute("SELECT timestamp FROM heartrate").fetchone()[0],
                         "2026-11-09T13:00:00+00:00")
        self.assertEqual(conn.execute("SELECT activity FROM workouts").fetchone()[0], "walking")
        self.assertEqual(db.get_sync_state(conn, "workout")["last_day"], "2026-11-09")
        conn.close()
        # Incremental: next run starts 3 days before last_day.
        fc2 = FakeClient()
        sync.sync(db_path=path, client=fc2, today=date(2026, 11, 10))
        self.assertEqual(dict(fc2.calls)["workout"]["start_date"], "2026-11-06")
        fc3 = FakeClient()
        sync.sync(recent=True, db_path=path, client=fc3, today=date(2026, 11, 10))
        self.assertEqual(dict(fc3.calls)["daily_sleep"]["start_date"], "2026-11-08")

    def test_sync_auth_error_is_reported_not_raised(self):
        class Bad(object):
            def get_all(self, *a, **k):
                raise OuraAuthError("nope; run oura_connect.py")
        res = sync.sync(days=1, db_path=Path(tempfile.mkdtemp()) / "t.db", client=Bad())
        self.assertFalse(res["ok"])
        self.assertIn("oura_connect.py", res["error"])


if __name__ == "__main__":
    unittest.main()
