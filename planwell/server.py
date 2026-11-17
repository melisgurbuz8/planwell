"""PlanWell local web server: static UI from web/ + JSON API (see SPEC "JSON API").

    python3 -m planwell.server --mock          # mock DB + mock calendar
    python3 -m planwell.server [--port 8765] [--no-browser]

Binds to 127.0.0.1 only.
"""
import argparse
import json
import mimetypes
import sys
import threading
import time
import traceback
import urllib.parse
import webbrowser
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from planwell import db, engine, gcal
from planwell.settings import CONFIG_PATH, load_config, public_config, update_config
from planwell.timeutil import get_tz, now_in, parse_iso

REPO_ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = REPO_ROOT / "web"
EVENT_CACHE_SECONDS = 60
MAX_BODY = 64 * 1024


class App(object):
    def __init__(self, mock=False, config_path=None, db_path=None, web_dir=None):
        self.mock = mock
        self.config_path = Path(config_path) if config_path else CONFIG_PATH
        self.db_path = str(db_path or db.default_path(mock))
        self.web_dir = Path(web_dir) if web_dir else WEB_DIR
        self._events = None  # (fetched_at, key, events, connected, message)
        self._lock = threading.Lock()
        self._analytics = {}  # name -> (signature, result)
        self._analytics_lock = threading.Lock()

    @property
    def mode(self):
        return "mock" if self.mock else "real"

    def config(self):
        return load_config(self.config_path)

    def events(self, config, now):
        """Events from now-6h to end of tomorrow -> (events, connected, message)."""
        tz = get_tz(config)
        day0 = now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
        start, end = now - timedelta(hours=6), day0 + timedelta(days=2)
        key = (tuple(config.get("google_calendar_ids") or []), day0.isoformat())
        with self._lock:
            c = self._events
            if c and c[1] == key and time.time() - c[0] < EVENT_CACHE_SECONDS:
                return c[2], c[3], c[4]
        if self.mock:
            evs, ok, msg = gcal.get_events(start, end, config, mock=True), True, None
        elif not gcal.is_connected():
            evs, ok, msg = [], False, ("Google Calendar isn't connected yet. Run "
                                      "`python3 -m planwell.gcal` once to connect it.")
        else:
            try:
                evs, ok, msg = gcal.list_events(start, end, config), True, None
            except gcal.CalendarNotConnected as e:
                evs, ok, msg = [], False, str(e)
            except Exception as e:
                evs, ok, msg = [], True, "Couldn't reach Google Calendar just now (%s)." % e
                return evs, ok, msg  # don't cache transient failures
        with self._lock:
            self._events = (time.time(), key, evs, ok, msg)
        return evs, ok, msg

    def invalidate_events(self):
        with self._lock:
            self._events = None

    # ------------------------------------------------------------ API
    def api_today(self):
        cfg = self.config()
        now = now_in(cfg)
        evs, ok, msg = self.events(cfg, now)
        out = engine.build_today(self.db_path, cfg, evs, now=now, mode=self.mode,
                                 calendar_connected=ok, calendar_message=msg)
        out["settings"] = self.public_settings(cfg)
        return 200, out

    def public_settings(self, cfg=None):
        """Settings for the browser: ntfy topic masked, plus ntfy_configured."""
        return public_config(cfg if cfg is not None else self.config(), self.config_path)

    def api_next48(self):
        cfg = self.config()
        now = now_in(cfg)
        evs, ok, msg = self.events(cfg, now)
        out = engine.build_next48(self.db_path, cfg, evs, now=now)
        out["calendar_connected"] = bool(ok)
        if msg:
            out["calendar_message"] = msg
        return 200, out

    def api_tomorrow(self):
        cfg = self.config()
        now = now_in(cfg)
        evs, ok, msg = self.events(cfg, now)
        out = engine.build_tomorrow(self.db_path, cfg, evs, now=now)
        out["calendar_connected"] = bool(ok)
        if msg:
            out["calendar_message"] = msg
        return 200, out

    def _analytics_signature(self, cfg):
        return (engine._db_signature(self.db_path), json.dumps(cfg, sort_keys=True))

    def api_analytics(self, name):
        """GET /api/insights | /api/backtest via planwell.analytics (cached per DB state + settings)."""
        try:
            from planwell import analytics
            fn = getattr(analytics, name)
        except Exception as e:
            return 503, {"error": "Analytics aren't available yet (%s)." % e}
        cfg = self.config()
        sig = self._analytics_signature(cfg)
        with self._analytics_lock:  # one computation at a time; slow-ish
            hit = self._analytics.get(name)
            if hit and hit[0] == sig:
                return 200, hit[1]
            res = fn(self.db_path, cfg)
            self._analytics[name] = (self._analytics_signature(cfg), res)
        return 200, res

    def invalidate_analytics(self):
        with self._analytics_lock:
            self._analytics.clear()

    def api_check(self, body):
        cfg = self.config()
        act = engine.norm_activity((body or {}).get("activity"), cfg)
        if act not in cfg["activities"]:
            return 400, {"error": "activity must be one of: %s" % ", ".join(cfg["activities"])}
        tz = get_tz(cfg)
        start = parse_iso(body.get("start"), tz) if body.get("start") else now_in(cfg)
        if start is None:
            return 400, {"error": "start must be an ISO date-time"}
        try:
            dur = int(round(float(body.get("duration_min", 30))))
        except (TypeError, ValueError):
            return 400, {"error": "duration_min must be a number"}
        if not (5 <= dur <= 300):
            return 400, {"error": "duration_min must be between 5 and 300"}
        event = None
        if body.get("event_id") or body.get("title"):
            event = {"id": body.get("event_id"), "title": body.get("title")}
        return 200, engine.check(self.db_path, cfg, act, start, dur, event=event)

    def api_sessions(self, query):
        cfg = self.config()
        act = (query.get("activity") or [None])[0] or None
        limit = (query.get("limit") or ["60"])[0]
        return 200, engine.sessions(self.db_path, cfg, activity=act, limit=limit)

    def api_session(self, wid):
        res = engine.session_detail(self.db_path, self.config(), wid)
        if res is None:
            return 404, {"error": "No session with id %s" % wid}
        return 200, res

    def api_settings_post(self, body):
        try:
            cfg = update_config(body, self.config_path)
        except ValueError as e:
            return 400, {"error": str(e)}
        self.invalidate_events()
        self.invalidate_analytics()
        return 200, self.public_settings(cfg)


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = "PlanWell/1.0"

        def log_message(self, fmt, *args):
            sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

        # --- security: only this Mac's own browser, on this exact address, may talk to us ---
        def _allowed_hosts(self):
            port = self.server.server_address[1]
            return {"127.0.0.1:%d" % port, "localhost:%d" % port}

        def _host_ok(self):
            # Blocks DNS-rebinding: a web page on evil.example re-pointed at 127.0.0.1 sends Host: evil.example
            return (self.headers.get("Host") or "").lower() in self._allowed_hosts()

        def _origin_ok(self):
            # Blocks cross-site writes: browsers attach Origin to cross-site POSTs.
            origin = self.headers.get("Origin")
            if origin is None:
                return True  # same-origin GETs, curl, scripts on this Mac
            return origin.lower() in {"http://" + h for h in self._allowed_hosts()}

        def _send(self, status, payload, ctype="application/json; charset=utf-8"):
            data = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                             "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
                             "form-action 'self'")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                raise ValueError("Request body too large.")
            raw = self.rfile.read(n) if n else b""
            if not raw.strip():
                return {}
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("Body must be a JSON object.")
            return data

        def _guard(self, fn, *a):
            try:
                status, payload = fn(*a)
            except ValueError as e:
                status, payload = 400, {"error": str(e)}
            except Exception as e:
                traceback.print_exc()
                status, payload = 500, {"error": "Something went wrong: %s" % e}
            self._send(status, payload)

        def do_GET(self):
            if not self._host_ok() or not self._origin_ok():
                return self._send(403, {"error": "Forbidden"})
            u = urllib.parse.urlparse(self.path)
            path, q = u.path, urllib.parse.parse_qs(u.query)
            if path == "/api/today":
                return self._guard(app.api_today)
            if path == "/api/next48":
                return self._guard(app.api_next48)
            if path == "/api/tomorrow":
                return self._guard(app.api_tomorrow)
            if path == "/api/insights":
                return self._guard(app.api_analytics, "insights")
            if path == "/api/backtest":
                return self._guard(app.api_analytics, "backtest")
            if path == "/api/sessions":
                return self._guard(app.api_sessions, q)
            if path.startswith("/api/session/"):
                wid = urllib.parse.unquote(path[len("/api/session/"):])
                return self._guard(app.api_session, wid)
            if path == "/api/settings":
                return self._guard(lambda: (200, app.public_settings()))
            if path.startswith("/api/"):
                return self._send(404, {"error": "Unknown endpoint %s" % path})
            return self._static(path)

        do_HEAD = do_GET

        def do_POST(self):
            path = urllib.parse.urlparse(self.path).path
            if not self._host_ok() or not self._origin_ok():
                return self._send(403, {"error": "Forbidden"})
            # JSON only: a cross-site <form> or "simple" fetch can't send application/json without a CORS
            # preflight, which this server never approves.
            if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
                return self._send(415, {"error": "Send JSON with Content-Type: application/json."})
            try:
                body = self._body()
            except (ValueError, UnicodeDecodeError) as e:
                return self._send(400, {"error": "Invalid JSON body: %s" % e})
            if path == "/api/check":
                return self._guard(app.api_check, body)
            if path == "/api/settings":
                return self._guard(app.api_settings_post, body)
            return self._send(404, {"error": "Unknown endpoint %s" % path})

        def _static(self, path):
            rel = urllib.parse.unquote(path).lstrip("/") or "index.html"
            root = app.web_dir.resolve()
            target = (root / rel).resolve()
            if target.is_dir():
                target = target / "index.html"
            try:
                target.relative_to(root)
            except ValueError:
                return self._send(403, {"error": "Forbidden"})
            if not target.is_file():
                if not (root / "index.html").exists() and rel == "index.html":
                    return self._send(200, b"<!doctype html><meta charset=utf-8><title>PlanWell</title>"
                                           b"<p>PlanWell API is running. The web UI (web/) is not built yet.",
                                      "text/html; charset=utf-8")
                return self._send(404, {"error": "Not found"})
            ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
                ctype += "; charset=utf-8"
            return self._send(200, target.read_bytes(), ctype)

    return Handler


def make_server(app, port=8765, host="127.0.0.1"):
    return ThreadingHTTPServer((host, port), make_handler(app))


def main(argv=None):
    ap = argparse.ArgumentParser(description="PlanWell local server")
    ap.add_argument("--mock", action="store_true", help="use data/mock.db + data/mock_calendar.json")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args(argv)
    mimetypes.add_type("application/javascript", ".js")
    mimetypes.add_type("text/css", ".css")
    app = App(mock=args.mock)
    try:
        httpd = make_server(app, args.port)
    except OSError as e:
        print("Could not start on port %d: %s (try --port 8766)" % (args.port, e))
        return 1
    url = "http://127.0.0.1:%d/" % args.port
    print("PlanWell (%s mode) running at %s  - Ctrl+C to stop" % (app.mode, url))
    if not Path(app.db_path).exists():
        print("Note: %s not found yet%s" % (app.db_path, " (run python3 -m planwell.mock_data)"
                                            if args.mock else " (run python3 -m planwell.sync)"))
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
