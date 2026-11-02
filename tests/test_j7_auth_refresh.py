"""Offline proofs for rotating, account-bound J7 credentials."""
import base64
import fcntl
import importlib
import json
import os
import threading
import time
from email.message import Message
from http.client import IncompleteRead
from urllib.error import HTTPError, URLError
from urllib.request import OpenerDirector

import pytest


NOW = 2_000_000_000


def jwt(exp, **claims):
    def encode(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    return "%s.%s.c2ln" % (encode({"alg": "HS256", "typ": "JWT"}), encode({
        "type": "access", "userId": "user-a", "sid": "session-a", "exp": exp, **claims}))


class Reply:
    def __init__(self, access, refresh="rotated-refresh", *, data=None, cookies=None):
        self.status = 200
        self.headers = Message()
        for cookie in (cookies if cookies is not None else [
                "__Host-j7_rt=%s; Path=/; Secure; HttpOnly" % refresh,
                "__Secure-j7_at=%s; Domain=.j7tracker.io; Path=/; Secure; HttpOnly" % access]):
            self.headers.add_header("Set-Cookie", cookie)
        self.data = json.dumps(data if data is not None else {
            "success": True, "token": access, "sessionId": access,
            "username": "test", "userId": "user-a", "expiresAt": (NOW + 7200) * 1000,
        }).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def read(self, limit):
        return self.data[:limit]


@pytest.fixture
def setup(tmp_path, monkeypatch):
    auth = importlib.import_module("sniper.feeds.j7_auth")
    old = jwt(NOW + 1800)
    new = jwt(NOW + 7200)
    path = tmp_path / ".j7.env"
    path.write_text("# private settings\nJ7_TOKEN=%s\nJ7_REFRESH_TOKEN=old-refresh\nOTHER='keep me'\n" % old)
    calls = []
    reply = [Reply(new)]

    def open_request(opener, request, timeout):
        calls.append((opener, request, timeout))
        result = reply[0]
        if callable(result):
            result = result()
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(OpenerDirector, "open", open_request)
    return auth, path, old, new, calls, reply


def read_env(path):
    from sniper.feeds.j7_feed import load_j7_env
    return load_j7_env(path)


def test_due_renewal_uses_exact_cookie_exchange_and_durable_pair(setup):
    auth, path, old, new, calls, _ = setup
    session = auth.J7Session(path, "J7_TOKEN", old)
    assert session.enabled
    assert session.refresh_if_due(now=NOW) == new
    assert session.access == new
    assert read_env(path)["J7_REFRESH_TOKEN"] == "rotated-refresh"
    assert "# private settings\n" in path.read_text()
    assert "OTHER='keep me'\n" in path.read_text()
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert os.stat(str(path) + ".lock").st_mode & 0o777 == 0o600
    _, request, timeout = calls[0]
    headers = {key.lower(): value for key, value in request.header_items()}
    assert request.full_url == "https://core.j7tracker.io/api/auth/refresh"
    assert request.get_method() == "POST" and request.data == b"{}" and timeout == 10
    assert headers["cookie"] == "__Host-j7_rt=old-refresh; __Secure-j7_at=%s" % old
    assert headers["x-j7-auth"] == "1"
    assert headers["content-type"] == "application/json"
    assert headers["origin"] == "https://j7tracker.io"
    assert headers["referer"] == "https://j7tracker.io/"
    assert "Mozilla/" in headers["user-agent"]
    status = session.status()
    assert status["last_success"] == NOW and status["count"] == 1
    assert status["next_attempt"] == NOW + 3600


def test_not_due_force_and_absent_refresh_are_distinct(setup):
    auth, path, _, new, calls, _ = setup
    path.write_text("J7_TOKEN=%s\nJ7_REFRESH_TOKEN=old-refresh\n" % jwt(NOW + 7200))
    session = auth.J7Session(path, "J7_TOKEN", jwt(NOW + 7200))
    assert session.refresh_if_due(now=NOW) is None and not calls
    # A forced response still has to extend the current deadline.
    setup[-1][0] = Reply(jwt(NOW + 10800))
    assert session.refresh_if_due(now=NOW, force=True) == jwt(NOW + 10800)
    path.write_text("J7_TOKEN=%s\n" % new)
    disabled = auth.J7Session(path, "J7_TOKEN", new)
    assert not disabled.enabled and disabled.refresh_if_due(now=NOW, force=True) is None


def test_expired_access_can_renew_and_account_two_updates_only_its_keys(setup):
    auth, path, _, new, _, _ = setup
    expired = jwt(NOW - 10)
    path.write_text("J7_TOKEN=manual\nJ7_TOKEN_2=%s\nJ7_REFRESH_TOKEN_2=old-refresh\n" % expired)
    session = auth.J7Session(path, "J7_TOKEN_2", expired)
    assert session.refresh_if_due(now=NOW) == new
    assert read_env(path) == {"J7_TOKEN": "manual", "J7_TOKEN_2": new,
                              "J7_REFRESH_TOKEN_2": "rotated-refresh"}


@pytest.mark.parametrize("code,retry,delay", [(429, "900", 900), (503, None, 300),
    (429, "Wed, 18 May 2033 03:48:20 GMT", 900), (429, "nonsense", 300), (302, None, 300)])
def test_http_failures_back_off_without_changing_credentials(setup, code, retry, delay):
    auth, path, old, _, calls, reply = setup
    headers = Message()
    if retry:
        headers["Retry-After"] = retry
    reply[0] = HTTPError("https://core.j7tracker.io/api/auth/refresh", code,
                         "secret old-refresh", headers, None)
    original = path.read_text()
    session = auth.J7Session(path, "J7_TOKEN", old)
    assert session.refresh_if_due(now=NOW) is None
    assert session.status()["next_attempt"] == NOW + delay
    assert session.refresh_if_due(now=NOW + delay - 1, force=True) is None
    assert len(calls) == 1 and path.read_text() == original and session.access == old
    assert "old-refresh" not in repr(session.status())


@pytest.mark.parametrize("error", [TimeoutError("old-refresh"), URLError("old-refresh"),
    IncompleteRead(b"private-body")])
def test_transport_errors_are_safe_and_retry_after_five_minutes(setup, error):
    auth, path, old, new, calls, reply = setup
    reply[0] = error
    session = auth.J7Session(path, "J7_TOKEN", old)
    assert session.refresh_if_due(now=NOW) is None
    assert session.status()["error_code"] == "network_error"
    assert session.status()["next_attempt"] == NOW + 300
    reply[0] = Reply(new)
    assert session.refresh_if_due(now=NOW + 300) == new
    assert len(calls) == 2


@pytest.mark.parametrize("code", [401, 403])
def test_revoked_refresh_requires_new_credentials_instead_of_repeated_posts(setup, code):
    auth, path, old, _, calls, reply = setup
    reply[0] = HTTPError("https://core.j7tracker.io/api/auth/refresh", code, "secret", Message(), None)
    session = auth.J7Session(path, "J7_TOKEN", old)
    assert session.refresh_if_due(now=NOW) is None
    assert session.status()["status"] == "reauth_required"
    assert session.refresh_if_due(now=NOW + 3600, force=True) is None
    assert len(calls) == 1 and session.access == old


@pytest.mark.parametrize("claims", [{"userId": "other"}, {"sid": "other"},
    {"type": "refresh"}, {"exp": NOW + 1800}, {"exp": NOW - 1},
    {"exp": float("nan")}, {"exp": True}, {"exp": 10 ** 1000}, {"sid": None}])
def test_wrong_identity_type_or_deadline_never_replaces_working_pair(setup, claims):
    auth, path, old, _, _, reply = setup
    bad = jwt(NOW + 7200, **claims) if "exp" not in claims else jwt(**claims)
    reply[0] = Reply(bad)
    before = path.read_text()
    session = auth.J7Session(path, "J7_TOKEN", old)
    assert session.refresh_if_due(now=NOW) is None
    assert session.access == old and path.read_text() == before
    assert session.status()["error_code"] == "invalid_response"


@pytest.mark.parametrize("kind", ["missing_refresh", "missing_access", "mismatched_cookie",
    "oversize_cookie",
    "invalid_jwt", "insecure_cookie", "old_refresh", "duplicate_cookie"])
def test_malformed_or_incomplete_exchange_does_not_write(setup, kind):
    auth, path, old, new, _, reply = setup
    response = Reply(new)
    if kind in ("missing_refresh", "missing_access"):
        name = "__Secure-j7_at" if kind == "missing_refresh" else "__Host-j7_rt"
        response = Reply(new, cookies=[c for c in response.headers.get_all("Set-Cookie") if c.startswith(name)])
    elif kind == "mismatched_cookie":
        response = Reply(new, cookies=["__Host-j7_rt=rotated; Path=/; Secure; HttpOnly",
            "__Secure-j7_at=%s; Domain=.j7tracker.io; Path=/; Secure; HttpOnly" % old])
    elif kind == "oversize_cookie":
        response = Reply(new, refresh="x" * 20000)
    elif kind == "invalid_jwt":
        response = Reply("bad.shape.signature")
    elif kind == "insecure_cookie":
        response = Reply(new, cookies=["__Host-j7_rt=rotated; Path=/; HttpOnly",
            "__Secure-j7_at=%s; Domain=.j7tracker.io; Path=/; Secure; HttpOnly" % new])
    elif kind == "old_refresh":
        response = Reply(new, refresh="old-refresh")
    elif kind == "duplicate_cookie":
        response.headers.add_header("Set-Cookie", "__Host-j7_rt=another; Path=/; Secure; HttpOnly")
    reply[0] = response
    before = path.read_text()
    session = auth.J7Session(path, "J7_TOKEN", old)
    assert session.refresh_if_due(now=NOW) is None
    assert path.read_text() == before and session.access == old


def test_status_does_not_wait_on_blocked_network_or_expose_mutable_state(setup):
    auth, path, old, new, _, reply = setup
    entered, release = threading.Event(), threading.Event()
    def blocked():
        entered.set()
        assert release.wait(2)
        return Reply(new)
    reply[0] = blocked
    session = auth.J7Session(path, "J7_TOKEN", old)
    worker = threading.Thread(target=lambda: session.refresh_if_due(now=NOW))
    worker.start()
    assert entered.wait(1)
    try:
        start = time.monotonic()
        snapshot = session.status()
        assert time.monotonic() - start < .1
        assert snapshot["status"] == "refreshing"
        snapshot["status"] = "corrupted"
        assert session.status()["status"] == "refreshing"
        assert old not in repr(snapshot) and "old-refresh" not in repr(snapshot)
        assert session.refresh_if_due(now=NOW, force=True) is None
    finally:
        release.set()
        worker.join(2)
    assert session.access == new


def test_file_lock_contention_is_nonblocking_and_does_not_post(setup):
    auth, path, old, _, calls, _ = setup
    session = auth.J7Session(path, "J7_TOKEN", old)
    with open(str(path) + ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        start = time.monotonic()
        assert session.refresh_if_due(now=NOW) is None
        assert time.monotonic() - start < .1 and not calls
        assert session.status()["error_code"] == "lock_busy"


def test_force_after_released_file_lock_still_renews_early(setup):
    auth, path, _, _, _, reply = setup
    old, new = jwt(NOW + 7200), jwt(NOW + 10800)
    path.write_text("J7_TOKEN=%s\nJ7_REFRESH_TOKEN=old-refresh\n" % old)
    reply[0] = Reply(new)
    session = auth.J7Session(path, "J7_TOKEN", old)
    with open(str(path) + ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert session.refresh_if_due(now=NOW, force=True) is None
    assert session.refresh_if_due(now=NOW, force=True) == new


def test_second_owner_adopts_durable_rotation_without_consuming_refresh_twice(setup):
    auth, path, old, new, calls, _ = setup
    first = auth.J7Session(path, "J7_TOKEN", old)
    second = auth.J7Session(path, "J7_TOKEN", old)
    assert first.refresh_if_due(now=NOW) == new
    assert second.refresh_if_due(now=NOW) == new
    assert second.access == new and len(calls) == 1


def test_temporary_credentials_use_the_ignored_env_prefix(setup, monkeypatch):
    auth, path, old, _, _, _ = setup
    replace = os.replace
    def private_replace(source, destination):
        from pathlib import Path
        assert Path(source).name.startswith(path.name + ".")
        assert os.stat(source).st_mode & 0o777 == 0o600
        return replace(source, destination)
    monkeypatch.setattr(os, "replace", private_replace)
    assert auth.J7Session(path, "J7_TOKEN", old).refresh_if_due(now=NOW)


def test_rotated_pair_retries_disk_before_any_further_http_and_merges_external_edits(setup, monkeypatch):
    auth, path, old, new, calls, _ = setup
    replace = os.replace
    def fail(*_):
        raise OSError("secret old-refresh")
    monkeypatch.setattr(os, "replace", fail)
    session = auth.J7Session(path, "J7_TOKEN", old)
    assert session.refresh_if_due(now=NOW) is None
    assert session.access == old and session.status()["status"] == "persistence_pending"
    assert session.status()["error_code"] == "persistence_failed"
    path.write_text(path.read_text() + "EXTERNAL=retained\n")
    monkeypatch.setattr(os, "replace", replace)
    assert session.refresh_if_due(now=NOW + 300, force=True) == new
    assert len(calls) == 1 and session.access == new
    assert read_env(path)["EXTERNAL"] == "retained"
    assert read_env(path)["J7_REFRESH_TOKEN"] == "rotated-refresh"


def test_other_settings_edited_during_http_survive_pair_write(setup):
    auth, path, old, new, _, reply = setup
    def exchange():
        path.write_text(path.read_text().replace("keep me", "operator edit"))
        return Reply(new)
    reply[0] = exchange
    assert auth.J7Session(path, "J7_TOKEN", old).refresh_if_due(now=NOW) == new
    assert read_env(path)["OTHER"] == "operator edit"


def test_response_session_id_can_be_opaque_without_weakening_jwt_binding(setup):
    auth, path, old, new, _, reply = setup
    data = json.loads(reply[0].data)
    data["sessionId"] = "opaque-server-session"
    reply[0].data = json.dumps(data).encode()
    assert auth.J7Session(path, "J7_TOKEN", old).refresh_if_due(now=NOW) == new


@pytest.mark.parametrize("body", [b"not-json", b'{"success":false}',
    b'{"token":"wrong","sessionId":"wrong"}', None])
def test_valid_cookie_rotation_survives_unreadable_or_mismatched_redundant_body(setup, body):
    auth, path, old, new, _, reply = setup
    if body is None:
        def truncated(_limit):
            raise IncompleteRead(b"private-body")
        reply[0].read = truncated
    else:
        reply[0].data = body
    session = auth.J7Session(path, "J7_TOKEN", old)
    assert session.refresh_if_due(now=NOW) == new
    assert session.access == new
    assert read_env(path)["J7_REFRESH_TOKEN"] == "rotated-refresh"


def test_unrelated_crlf_lines_remain_byte_identical(setup):
    auth, path, old, new, _, _ = setup
    path.write_bytes(("J7_TOKEN=%s\r\nJ7_REFRESH_TOKEN=old-refresh\r\n# keep\r\nOTHER='x'\r\n" % old).encode())
    assert auth.J7Session(path, "J7_TOKEN", old).refresh_if_due(now=NOW) == new
    assert b"# keep\r\nOTHER='x'\r\n" in path.read_bytes()


def test_newer_operator_login_during_http_wins_over_pending_rotation(setup):
    auth, path, old, new, calls, reply = setup
    operator = jwt(NOW + 15000, sid="operator-login")
    def exchange():
        path.write_text("J7_TOKEN=%s\nJ7_REFRESH_TOKEN=operator-refresh\n" % operator)
        return Reply(new)
    reply[0] = exchange
    session = auth.J7Session(path, "J7_TOKEN", old)
    assert session.refresh_if_due(now=NOW) == operator
    assert session.access == operator and len(calls) == 1
    assert read_env(path)["J7_REFRESH_TOKEN"] == "operator-refresh"


def test_wrong_account_external_change_is_preserved_and_never_posted(setup):
    auth, path, old, _, calls, _ = setup
    session = auth.J7Session(path, "J7_TOKEN", old)
    other = jwt(NOW + 15000, userId="other")
    path.write_text("J7_TOKEN=%s\nJ7_REFRESH_TOKEN=other-refresh\n" % other)
    assert session.refresh_if_due(now=NOW) is None
    assert session.access == old and read_env(path)["J7_TOKEN"] == other and not calls


def test_redirect_handler_refuses_forwarding_secrets(setup):
    auth, _, _, _, _, _ = setup
    assert auth._NoRedirect().redirect_request(None, None, 302, "moved", Message(),
                                               "https://evil.invalid") is None
