"""One durable owner of a J7 rotating refresh-cookie chain; no live-path I/O."""
from __future__ import annotations

import base64
import fcntl
import json
import math
import os
import re
import tempfile
import threading
import time
from email.utils import parsedate_to_datetime
from http.client import HTTPException
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener


_LINE = re.compile(r"^\s*([A-Z0-9_]+)\s*=\s*(.*?)\s*$")
_COOKIE = re.compile(r"[\x21\x23-\x2b\x2d-\x3a\x3c-\x5b\x5d-\x7e]+\Z")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def _claims(token):
    if not isinstance(token, str) or len(token) > 16384:
        raise ValueError
    parts = token.split(".")
    if len(parts) != 3 or any(not re.fullmatch(r"[A-Za-z0-9_-]+", p) for p in parts):
        raise ValueError
    decoded = [base64.b64decode(p + "=" * (-len(p) % 4), altchars=b"-_", validate=True)
               for p in parts]
    header, body = json.loads(decoded[0]), json.loads(decoded[1])
    if (not isinstance(header, dict) or not isinstance(body, dict)
            or body.get("type") != "access"
            or not isinstance(body.get("userId"), (str, int)) or isinstance(body.get("userId"), bool)
            or not body["userId"] or not isinstance(body.get("sid"), str) or not body["sid"]
            or not isinstance(body.get("exp"), (int, float)) or isinstance(body["exp"], bool)
            or not math.isfinite(body["exp"]) or body["exp"] <= 0):
        raise ValueError
    return body


def _cookie_value(value):
    return isinstance(value, str) and 0 < len(value) <= 16384 and bool(_COOKIE.fullmatch(value))


def _read(path):
    with path.open(newline="") as stream:
        text = stream.read()
    values = {}
    for line in text.splitlines():
        match = _LINE.match(line)
        if match:
            values[match[1]] = match[2].strip().strip('"').strip("'")
    return text, values


def _retry_delay(headers, now):
    value = headers.get("Retry-After") if headers else None
    try:
        delay = float(value)
    except (TypeError, ValueError):
        try:
            delay = parsedate_to_datetime(value).timestamp() - now
        except (TypeError, ValueError, OverflowError):
            delay = 300
    return max(300, delay) if math.isfinite(delay) else 300


class J7Session:
    """Refresh I/O belongs only on a daemon; status always copies an atomic snapshot."""

    def __init__(self, path, key, token):
        if key not in ("J7_TOKEN", "J7_TOKEN_2"):
            raise ValueError("unsupported J7 token key")
        self._path = Path(path)
        self._key = key
        self._refresh_key = "J7_REFRESH_TOKEN" + ("_2" if key.endswith("_2") else "")
        self.access = token
        self._mutex = threading.Lock()
        self._pending = None
        self._revoked = None
        error = None
        try:
            _, env = _read(self._path)
        except (OSError, UnicodeError):
            env, error = {}, "env_read_failed"
        self.enabled = bool(env.get(self._refresh_key))
        try:
            due = _claims(token)["exp"] - 3600
        except (ValueError, TypeError, KeyError, OverflowError):
            due = None
        self._snapshot = dict(enabled=self.enabled, status="ready" if self.enabled else "disabled",
                              last_attempt=None, last_success=None, next_attempt=due,
                              error_code=error, count=0)

    def status(self):
        return self._snapshot.copy()

    def _state(self, **changes):
        self._snapshot = {**self._snapshot, "enabled": self.enabled, **changes}

    def _pair(self, env):
        return env.get(self._key), env.get(self._refresh_key)

    def _adopt(self, pair, now, *, own=False):
        self.access = pair[0]
        self._pending = self._revoked = None
        self.enabled = bool(pair[1])
        self._state(status="ready", error_code=None, last_success=now,
                    next_attempt=_claims(pair[0])["exp"] - 3600,
                    count=self._snapshot["count"] + int(own))
        return self.access

    def _newer(self, pair, now):
        try:
            old, new = _claims(self.access), _claims(pair[0])
            return (_cookie_value(pair[1]) and new["userId"] == old["userId"]
                    and new["exp"] > max(now, old["exp"]))
        except (ValueError, TypeError, KeyError, OverflowError):
            return False

    def _sync_directory(self):
        fd = os.open(self._path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _persist(self, base, new):
        # Re-read after HTTP and before replace; cooperating owners share the env lock.
        for _ in range(3):
            text, env = _read(self._path)
            if self._pair(env) != base:
                return False
            values = {self._key: new[0], self._refresh_key: new[1]}
            found, lines = set(), []
            for line in text.splitlines(keepends=True):
                match = _LINE.match(line)
                if match and match[1] in values:
                    found.add(match[1])
                    line = "%s=%s\n" % (match[1], values[match[1]])
                lines.append(line)
            output = "".join(lines)
            for key in values.keys() - found:
                if output and not output.endswith("\n"):
                    output += "\n"
                output += "%s=%s\n" % (key, values[key])
            fd, temporary = tempfile.mkstemp(prefix=".j7.env.", dir=self._path.parent)
            try:
                with os.fdopen(fd, "w") as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    stream.write(output)
                    stream.flush()
                    os.fsync(stream.fileno())
                if _read(self._path)[0] != text:
                    continue
                os.replace(temporary, self._path)
                self._sync_directory()
                return True
            finally:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
        raise OSError

    def _finish_pending(self, now):
        base, new = self._pending
        try:
            _, env = _read(self._path)
            current = self._pair(env)
            if current == new:
                # A previous replace may have succeeded before directory fsync failed.
                self._sync_directory()
                return self._adopt(new, now, own=True)
            if current != base:
                if self._newer(current, now):
                    return self._adopt(current, now)
                self._state(status="persistence_pending", error_code="credential_conflict",
                            next_attempt=now + 300)
                return None
            if self._persist(base, new):
                return self._adopt(new, now, own=True)
            # A target-pair edit detected immediately before replacement wins too.
            _, env = _read(self._path)
            if self._newer(self._pair(env), now):
                return self._adopt(self._pair(env), now)
            self._state(status="persistence_pending", error_code="credential_conflict",
                        next_attempt=now + 300)
        except (OSError, UnicodeError):
            self._state(status="persistence_pending", error_code="persistence_failed",
                        next_attempt=now + 300)
        return None

    def _exchange(self, access, refresh, now):
        from sniper.feeds.j7_feed import HEADERS
        headers = {**HEADERS, "Content-Type": "application/json", "X-J7-Auth": "1",
                   "Cookie": "__Host-j7_rt=%s; __Secure-j7_at=%s" % (refresh, access)}
        request = Request("https://core.j7tracker.io/api/auth/refresh", b"{}", headers, method="POST")
        with build_opener(_NoRedirect()).open(request, timeout=10) as response:
            if response.status != 200:
                raise HTTPError(request.full_url, response.status, "", response.headers, None)
            # The rotating pair is in headers; a truncated redundant body must not lose it.
            cookies = {}
            for field in response.headers.get_all("Set-Cookie", []):
                if len(field) > 32768:
                    raise ValueError
                parsed = SimpleCookie()
                parsed.load(field)
                for name, cookie in parsed.items():
                    if name not in ("__Host-j7_rt", "__Secure-j7_at"):
                        continue
                    if (name in cookies or not _cookie_value(cookie.value)
                            or not cookie["secure"] or not cookie["httponly"] or cookie["path"] != "/"
                            or (name == "__Host-j7_rt" and cookie["domain"])
                            or (name == "__Secure-j7_at" and cookie["domain"].lstrip(".") != "j7tracker.io")):
                        raise ValueError
                    cookies[name] = cookie.value
            token, rotated = cookies.get("__Secure-j7_at"), cookies.get("__Host-j7_rt")
            if not token or not rotated or rotated == refresh:
                raise ValueError
            old, new = _claims(access), _claims(token)
            if (new["userId"] != old["userId"] or new["sid"] != old["sid"]
                    or new["exp"] <= max(now, old["exp"])):
                raise ValueError
            return token, rotated

    def refresh_if_due(self, now=None, force=False):
        now = time.time() if now is None else now
        if not self.enabled or not self._mutex.acquire(blocking=False):
            return None
        lock = None
        try:
            lock = os.open(str(self._path) + ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            os.fchmod(lock, 0o600)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self._state(error_code="lock_busy")
                return None
            if self._pending:
                return self._finish_pending(now)
            _, env = _read(self._path)
            pair = self._pair(env)
            if pair[0] != self.access:
                if self._newer(pair, now):
                    return self._adopt(pair, now)
                self._state(status="backoff", error_code="credential_conflict", next_attempt=now + 300)
                return None
            if not pair[1]:
                self.enabled = False
                self._state(status="disabled", error_code=None, next_attempt=None)
                return None
            claims = _claims(self.access)
            if not _cookie_value(pair[1]):
                raise ValueError
            if self._revoked == pair:
                return None
            deadline = self._snapshot["next_attempt"]
            if (self._snapshot["status"] == "backoff" and deadline is not None and now < deadline):
                return None
            if not force and claims["exp"] - now > 3600:
                return None
            self._state(status="refreshing", last_attempt=now, error_code=None)
            try:
                rotated = self._exchange(self.access, pair[1], now)
            except HTTPError as error:
                revoked = error.code in (401, 403)
                self._revoked = pair if revoked else None
                self._state(status="reauth_required" if revoked else "backoff",
                            error_code="http_%d" % error.code,
                            next_attempt=None if revoked else now + _retry_delay(error.headers, now))
                return None
            except (OSError, URLError, HTTPException):
                self._state(status="backoff", error_code="network_error", next_attempt=now + 300)
                return None
            except (ValueError, TypeError, KeyError, OverflowError, CookieError):
                self._state(status="backoff", error_code="invalid_response", next_attempt=now + 300)
                return None
            # Keep this privately until BOTH rotating credentials survive together.
            self._pending = pair, rotated
            return self._finish_pending(now)
        except (OSError, UnicodeError):
            self._state(status="backoff", error_code="env_io_failed", next_attempt=now + 300)
        except (ValueError, TypeError, KeyError, OverflowError):
            self._state(status="backoff", error_code="invalid_credentials", next_attempt=now + 300)
        finally:
            if lock is not None:
                os.close(lock)
            self._mutex.release()
        return None
