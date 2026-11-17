"""Minimal Oura API v2 client (standard library only).

- Loads secrets/oura_app.json  {client_id, client_secret}
  and   secrets/oura_tokens.json {access_token, refresh_token, expires_in, obtained_at, ...}
  (the format written by oura_connect.py; obtained_at is a unix epoch int).
- Refreshes the access token when it is within 1 hour of expiry, or after a 401.
  Oura refresh tokens are SINGLE-USE, so the new token file is written atomically
  immediately after every refresh.
- Paginates via `next_token`; honours 429 Retry-After with backoff; retries 5xx.

This module never logs or prints response bodies (Oura data must stay private).
"""
import http.client
import json
import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from planwell.db import REPO_ROOT

TOKEN_URL = "https://api.ouraring.com/oauth/token"
API_BASE = "https://api.ouraring.com/v2/usercollection"
SECRETS_DIR = REPO_ROOT / "secrets"
REFRESH_MARGIN_S = 3600
MAX_RETRIES = 5
TIMEOUT_S = 60

RECONNECT_HINT = "Please run `python3 oura_connect.py` from the PlanWell folder to reconnect your Oura account."


class OuraAuthError(RuntimeError):
    """Tokens are missing/invalid; the user must re-run oura_connect.py."""


class OuraAPIError(RuntimeError):
    """Non-auth API failure (after retries)."""

    def __init__(self, msg, status=None):
        RuntimeError.__init__(self, msg)
        self.status = status


def _write_json_atomic(path, obj):
    """Write JSON to a temp file in the same dir, fsync, then os.replace (atomic)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp_", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class OuraClient(object):
    """Usage:  c = OuraClient(); rows = c.get_all("workout", start_date=..., end_date=...)

    `urlopen`, `sleep` and `now` are injectable for tests.
    """

    def __init__(self, secrets_dir=None, urlopen=None, sleep=None, now=None):
        self.secrets_dir = Path(secrets_dir) if secrets_dir else SECRETS_DIR
        self.app_file = self.secrets_dir / "oura_app.json"
        self.token_file = self.secrets_dir / "oura_tokens.json"
        self._urlopen = urlopen or urllib.request.urlopen
        self._sleep = sleep or time.sleep
        self._now = now or time.time
        self.app = self._load(self.app_file, ("client_id", "client_secret"), "Oura app credentials")
        self.tok = self._load(self.token_file, ("access_token", "refresh_token"), "Oura login tokens")

    # ---- token handling -------------------------------------------------
    @staticmethod
    def _load(path, required, what):
        if not path.exists():
            raise OuraAuthError("%s not found (%s). %s" % (what, path.name, RECONNECT_HINT))
        try:
            data = json.loads(path.read_text())
        except ValueError:
            raise OuraAuthError("%s file %s is unreadable. %s" % (what, path.name, RECONNECT_HINT))
        missing = [k for k in required if not data.get(k)]
        if missing:
            raise OuraAuthError("%s file %s is missing %s. %s"
                                % (what, path.name, ", ".join(missing), RECONNECT_HINT))
        return data

    def _expires_soon(self):
        obtained = float(self.tok.get("obtained_at") or 0)
        expires_in = float(self.tok.get("expires_in") or 0)
        return self._now() >= obtained + expires_in - REFRESH_MARGIN_S

    def refresh(self):
        """Exchange the (single-use) refresh token and persist the new tokens atomically."""
        body = urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "refresh_token": self.tok["refresh_token"],
            "client_id": self.app["client_id"],
            "client_secret": self.app["client_secret"],
        }).encode()
        req = urllib.request.Request(TOKEN_URL, data=body, method="POST",
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            with self._urlopen(req, timeout=TIMEOUT_S) as r:
                new = json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (400, 401, 403):
                raise OuraAuthError("Oura rejected the saved login (refresh failed, HTTP %d). %s"
                                    % (e.code, RECONNECT_HINT))
            raise OuraAPIError("Oura token refresh failed (HTTP %d). Try again later." % e.code, e.code)
        except urllib.error.URLError as e:
            raise OuraAPIError("Could not reach Oura to refresh the login (%s)." % e.reason)
        if not new.get("access_token"):
            raise OuraAuthError("Oura token refresh returned no access token. " + RECONNECT_HINT)
        # Single-use: if Oura omitted a refresh_token, keep the old one (best effort).
        new.setdefault("refresh_token", self.tok["refresh_token"])
        new["obtained_at"] = int(self._now())
        _write_json_atomic(self.token_file, new)
        self.tok = new

    # ---- HTTP -----------------------------------------------------------
    def get(self, path, **params):
        """GET one page of /v2/usercollection/<path>. Returns parsed JSON."""
        if self._expires_soon():
            self.refresh()
        url = "%s/%s?%s" % (API_BASE, path, urllib.parse.urlencode(params))
        refreshed_after_401 = False
        attempt = 0
        while True:
            req = urllib.request.Request(url, headers={
                "Authorization": "Bearer %s" % self.tok["access_token"],
                "Accept": "application/json"})
            try:
                with self._urlopen(req, timeout=TIMEOUT_S) as r:
                    return json.loads(r.read())
            except urllib.error.HTTPError as e:
                if e.code == 401 and not refreshed_after_401:
                    refreshed_after_401 = True
                    self.refresh()
                    continue
                if e.code in (401, 403):
                    raise OuraAuthError("Oura refused access to '%s' (HTTP %d). %s"
                                        % (path, e.code, RECONNECT_HINT))
                if (e.code == 429 or e.code >= 500) and attempt < MAX_RETRIES:
                    self._sleep(self._retry_delay(e, attempt))
                    attempt += 1
                    continue
                raise OuraAPIError("Oura request for '%s' failed (HTTP %d)." % (path, e.code), e.code)
            except urllib.error.URLError as e:
                if attempt < MAX_RETRIES:
                    self._sleep(min(2 ** attempt, 30))
                    attempt += 1
                    continue
                raise OuraAPIError("Could not reach Oura (%s)." % e.reason)
            except (TimeoutError, ConnectionError, http.client.HTTPException, OSError) as e:
                # A slow or dropped response (socket read timeout, reset, incomplete read) isn't wrapped
                # in URLError, so retry it the same way instead of failing the whole sync.
                if attempt < MAX_RETRIES:
                    self._sleep(min(2 ** attempt, 30))
                    attempt += 1
                    continue
                raise OuraAPIError("Oura didn't respond in time (%s). Will retry on the next sync." % e)

    @staticmethod
    def _retry_delay(err, attempt):
        """Retry-After seconds if given (capped at 5 min), else exponential backoff."""
        ra = err.headers.get("Retry-After") if err.headers is not None else None
        if ra:
            try:
                return min(max(float(ra), 1.0), 300.0)
            except ValueError:
                pass
        return min(2 ** attempt * 2, 60)

    def get_all(self, path, **params):
        """GET every page (following next_token) and return the combined `data` list."""
        out = []
        token = None
        while True:
            p = dict(params)
            if token:
                p["next_token"] = token
            page = self.get(path, **p)
            out.extend(page.get("data") or [])
            token = page.get("next_token")
            if not token:
                return out
