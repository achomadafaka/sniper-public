"""Credential handoff must not relabel an old socket's roster or block receipt ingress."""
import ast
import json
import pathlib
import sys
import threading
import time
import types

import pytest

from _source_guard import code_of
from sniper.feeds import j7_feed as J


class Session:
    enabled = True

    def __init__(self, token="new-access"):
        self.token = token
        self.forced = []

    def refresh_if_due(self, now=None, force=False):
        self.forced.append(force)
        return self.token

    def status(self):
        return {"enabled": self.enabled, "status": "ready", "last_success": 123}


class StopLoop(BaseException):
    pass


def socket_feed(monkeypatch, session):
    handlers, threads, sockets = {}, [], []

    class Socket:
        sid = "fake-sid"
        connected = False

        def __init__(self, **kwargs):
            self.disconnects = 0
            self.auth = None
            self.connect_action = sockets[0].connect_action if sockets else lambda: None
            self.wait_action = sockets[0].wait_action if sockets else lambda: None
            sockets.append(self)

        def event(self, fn):
            handlers[fn.__name__] = fn
            return fn

        def on(self, name):
            def register(fn):
                handlers[name] = fn
                return fn
            return register

        def connect(self, *args, **kwargs):
            self.auth = kwargs["auth"]
            self.connected = True
            handlers["connect"]()
            self.connect_action()

        def wait(self):
            self.wait_action()

        def disconnect(self):
            self.disconnects += 1
            self.connected = False
            handlers["disconnect"]()

        def shutdown(self):
            if self.connected:
                self.disconnect()

    monkeypatch.setitem(sys.modules, "socketio", types.SimpleNamespace(Client=Socket))
    monkeypatch.setattr(J.threading.Thread, "start", lambda self: threads.append(self))
    feed = J.J7WalletsFeed("old-access", session=session).start()
    return feed, sockets[0], handlers, threads


def roster(handlers):
    handlers["tracked_fomo"]({"fomo_users": [{"fomo_user_id": "old-user"}]})
    handlers["tracked_pump"]({"pump_users": [{"address": "old-wallet"}]})


def relay():
    return {"kind": "trade", "data": {"id": "queued", "userId": "old-user", "side": "buy",
            "token": {"address": "mint", "networkId": "solana"}}}


def test_rotation_waits_for_old_socket_then_clears_rosters_but_keeps_receipts(monkeypatch):
    feed, sio, handlers, _ = socket_feed(monkeypatch, Session())
    feed._mono_fn = lambda: 12345
    feed.observe(relay())
    clock, anchors, run_id = feed._receipt_clock, feed._receipt_anchors, feed._capture_run_id
    attempts = []

    def connected():
        attempts.append(feed._sio.auth()["token"])
        if len(attempts) == 2:
            status = feed.status()
            assert status["tracked_users"] is None and status["pump_tracked_users"] is None
            assert status["tracked_count"] is None and status["pump_tracked_count"] is None
            assert status["tracked_seen_at"] is None and status["pump_tracked_seen_at"] is None
            assert status["last_error"] is None and status["last_error_at"] is None
            assert status["account_fingerprint"] == J.account_fingerprint("new-access")
            raise StopLoop

    def waiting():
        roster(handlers)
        feed._set(last_error="stale socket error", last_error_at="old-error", last_event_at="old-event")
        feed._refresh_auth(sio)
        assert feed._token == "old-access", "callbacks still belong to the old socket"
        assert feed.status()["tracked_users"][0]["source_user_id"] == "old-user"
        assert feed.status()["account_fingerprint"] == J.account_fingerprint("old-access")
        assert sio.auth()["token"] == "old-access"

    sio.connect_action, sio.wait_action = connected, waiting
    monkeypatch.setattr(J.time, "sleep", lambda seconds: pytest.fail("planned rotation must reconnect immediately"))
    with pytest.raises(StopLoop):
        feed._thread._target()
    assert attempts == ["old-access", "new-access"]
    assert sio.disconnects == 1
    assert (feed._receipt_clock, feed._receipt_anchors, feed._capture_run_id) == (clock, anchors, run_id)
    item = feed.drain()[0][0]
    assert item["id"] == "queued" and item["received_mono_ns"] == 12345
    assert feed.take_receipts()[0]["received_mono_ns"] == 12345


def test_pending_rotation_disconnects_an_old_transport_that_reconnects_late(monkeypatch):
    feed, sio, handlers, _ = socket_feed(monkeypatch, Session())
    feed._refresh_auth(sio)
    assert sio.disconnects == 0
    sio.connected = True
    handlers["connect"]()
    assert sio.disconnects == 1, "a reconnect racing the refresh must not strand the pending token"
    assert feed._token == "old-access"


def test_pending_rotation_aborts_a_disconnected_socket_reconnect_owner():
    import socketio
    sio = socketio.Client()
    started = threading.Event()
    sio._reconnect_abort = threading.Event()

    def reconnecting():
        started.set()
        sio._reconnect_abort.wait(2)

    reconnect = threading.Thread(target=reconnecting)
    sio._reconnect_task = reconnect
    reconnect.start()
    try:
        assert started.wait(1)
        feed = J.J7WalletsFeed("old-access", session=Session())
        feed._refresh_auth(sio)
        reconnect.join(0.2)
        assert not reconnect.is_alive(), "old-token reconnect must end so the pending token can activate"
        assert feed._token == "old-access" and feed._pending_token == "new-access"
    finally:
        sio._reconnect_abort.set()
        reconnect.join(1)


def test_delayed_old_socket_callbacks_cannot_repopulate_a_rotated_roster(monkeypatch):
    feed, sio, handlers, _ = socket_feed(monkeypatch, Session())
    old_handlers = dict(handlers)
    attempts, snapshots = [], []

    def connected():
        attempts.append(1)
        if len(attempts) == 2:
            before = feed.status()
            roster(old_handlers)
            old_handlers["connect_error"]({"message": "Invalid token"})
            old_handlers["auth_error"]({"message": "old rejection"})
            old_handlers["disconnect"]()
            snapshots.extend([before, feed.status()])
            raise StopLoop

    def waiting():
        feed._refresh_auth(sio)

    sio.connect_action, sio.wait_action = connected, waiting
    with pytest.raises(StopLoop):
        feed._thread._target()
    assert snapshots[0] == snapshots[1], "late callbacks must retain their old credential owner"


def test_refresh_forces_auth_rejection_and_safe_worker_failure(monkeypatch):
    session = Session()
    feed, sio, _, _ = socket_feed(monkeypatch, session)
    feed._set(status="auth_error")
    feed._refresh_auth(sio)
    assert session.forced == [True]

    def failed(**kwargs):
        raise RuntimeError("new-access private-refresh-cookie")

    session.refresh_if_due = failed
    feed._refresh_auth(sio)
    status = feed.status()
    assert "new-access" not in json.dumps(status)
    assert "private-refresh-cookie" not in json.dumps(status)
    assert status["auth_refresh"]["last_error"] == "RuntimeError"


def test_blocked_refresh_does_not_hold_feed_lock_or_block_ingress():
    started, release = threading.Event(), threading.Event()
    session = Session()

    def blocked(**kwargs):
        started.set()
        assert release.wait(2)
        return "new-access"

    session.refresh_if_due = blocked
    feed = J.J7WalletsFeed("old-access", session=session)
    thread = threading.Thread(target=feed._refresh_auth,
                              args=(types.SimpleNamespace(connected=False, shutdown=lambda: None),))
    thread.start()
    try:
        assert started.wait(1)
        before = time.monotonic()
        feed.observe(relay())
        assert feed.drain()[0][0]["id"] == "queued"
        assert feed.status()["auth_refresh"]["enabled"] is True
        assert time.monotonic() - before < 0.2
    finally:
        release.set()
        thread.join(1)


def test_auth_owner_is_daemon_and_disabled_sessions_start_no_worker(monkeypatch):
    feed, sio, _, threads = socket_feed(monkeypatch, Session())
    owners = [thread for thread in threads if thread is not feed._thread]
    assert len(owners) == 1 and owners[0].daemon
    calls = []
    feed._refresh_auth = lambda socket: calls.append(socket)

    def sleep(seconds):
        assert seconds == 60
        raise StopLoop

    monkeypatch.setattr(J.time, "sleep", sleep)
    with pytest.raises(StopLoop):
        owners[0]._target()
    assert calls == [sio]
    disabled = Session()
    disabled.enabled = False
    other, _, _, threads = socket_feed(monkeypatch, disabled)
    assert threads == [other._thread]


def test_rotating_one_account_preserves_other_account_identity_and_roster():
    session = Session()
    feed = J.J7AccountsFeed(["old-access", "other-access"], sessions=[session, None])
    first, second = feed._feeds
    first._set(tracked_users=[{"source_user_id": "first"}], tracked_seen_at="first-stamp")
    second._set(tracked_users=[{"source_user_id": "second"}], tracked_seen_at="second-stamp")
    first._refresh_auth(types.SimpleNamespace(connected=False, shutdown=lambda: None))
    status = feed.status()["accounts"]
    assert status[0]["auth_refresh"]["enabled"] is True
    assert status[1]["auth_refresh"]["enabled"] is False
    assert status[0]["account_fingerprint"] == J.account_fingerprint("old-access")
    assert status[1]["account_fingerprint"] == J.account_fingerprint("other-access")
    assert second._token == "other-access" and status[1]["tracked_seen_at"] == "second-stamp"
    assert "old-access" not in json.dumps(status) and "other-access" not in json.dumps(status)


def test_factory_binds_only_the_saved_refresh_secret_to_its_matching_slot(tmp_path, monkeypatch):
    from sniper.settings import memecoin
    path = tmp_path / ".j7.env"
    path.write_text("J7_TOKEN=first\nJ7_TOKEN_2=second\nJ7_REFRESH_TOKEN_2=private-refresh\n")
    monkeypatch.setenv("J7_ENV_FILE", str(path))
    monkeypatch.setenv("J7_REFRESH_TOKEN", "process-only-refresh")
    monkeypatch.setattr(memecoin, "MEMECOIN_SIGNAL_SOURCE", "j7")
    created = []

    class SavedSession(Session):
        def __init__(self, path, key, token):
            created.append((str(path), key, token))

    monkeypatch.setitem(sys.modules, "sniper.feeds.j7_auth", types.SimpleNamespace(J7Session=SavedSession))
    monkeypatch.setattr(J.J7WalletsFeed, "start", lambda self: self)
    monkeypatch.setattr(J, "_SINGLETON", None)
    feed = J.j7_feed_or_none()
    assert created == [(str(path), "J7_TOKEN_2", "second")]
    assert feed._feeds[0]._session is None and feed._feeds[1]._session is not None


def test_single_account_heartbeat_retains_safe_renewal_status(tmp_path):
    from sniper.services.memecoin_runner import write_heartbeat
    feed = J.J7WalletsFeed("private-access", session=Session())
    path = tmp_path / "heartbeat.json"
    assert write_heartbeat({}, True, path=str(path), j7_feed_status=feed.status()) is True
    payload = json.loads(path.read_text())
    assert payload["j7_feed"]["auth_refresh"] == {
        "enabled": True, "status": "ready", "last_success": 123}
    assert "private-access" not in path.read_text()


def runner_node(name):
    tree = ast.parse(code_of(pathlib.Path("scripts/memecoin_runner_loop.py")))
    return next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == name)


@pytest.mark.parametrize("multiple", [False, True])
def test_real_runner_persists_roster_and_identity_from_one_snapshot(monkeypatch, multiple):
    from sniper.persistence import db_memecoin
    from sniper.util import clock
    writes, unseen, warnings = [], [], []
    monkeypatch.setattr(db_memecoin, "replace_j7_tracked", lambda *args, **kwargs: writes.append((args, kwargs)))
    monkeypatch.setattr(db_memecoin, "mark_j7_list_unseen", lambda *args, **kwargs: unseen.append((args, kwargs)))
    monkeypatch.setattr(clock, "real_utc_now_iso", lambda: "now")
    account = {"account_fingerprint": "snapshot-fingerprint", "tracked_users": [], "tracked_count": 0,
               "tracked_seen_at": "frame-time", "pump_tracked_users": None, "pump_tracked_seen_at": None}

    class Feed:
        def status(self):
            return {"accounts": [account]} if multiple else account

        def account_fingerprints(self):
            pytest.fail("a separate identity read can race rotation")

    namespace = {"_p": warnings.append}
    exec(compile(ast.Module(body=[runner_node("_persist_j7_tracked")], type_ignores=[]), "runner-persist", "exec"), namespace)
    namespace["_persist_j7_tracked"](object(), Feed())
    assert not warnings
    assert len(writes) == 1 and len(unseen) == 1
    assert writes[0][1]["account_fingerprint"] == "snapshot-fingerprint"
    assert writes[0][1]["seen_at"] == "frame-time"
    assert unseen[0][0][2] == "pump" and unseen[0][1]["account_fingerprint"] == "snapshot-fingerprint"


@pytest.mark.parametrize("multiple", [False, True])
def test_real_health_cycle_reads_current_expiry_and_one_refresh_status(monkeypatch, multiple):
    from sniper.services import memecoin_alerts
    pages, health, warnings = [], [], []
    monkeypatch.setattr(memecoin_alerts, "note_j7_accounts", lambda status, **kwargs: health.append(status))
    monkeypatch.setattr(memecoin_alerts, "note_j7_token_expiry", lambda expiries, **kwargs: pages.append((expiries, kwargs)))
    tree = ast.parse(code_of(pathlib.Path("scripts/memecoin_runner_loop.py")))
    block = next(node for node in ast.walk(tree) if isinstance(node, ast.If)
                 and ast.unparse(node.test) == "_j7_feed is not None"
                 and any(isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                         and call.func.id == "note_j7_accounts" for call in ast.walk(node)))

    class Feed:
        reads = 0
        expiry = 100

        def status(self):
            self.reads += 1
            account = {"auth_refresh": {"enabled": True}}
            return {"accounts": [account, {"auth_refresh": {"enabled": False}}]} if multiple else account

        def token_expiries(self):
            return [(1, self.expiry)]

    feed = Feed()
    namespace = {"_j7_feed": feed, "time": time, "_p": warnings.append, "traceback": __import__("traceback")}
    compiled = compile(ast.Module(body=[block], type_ignores=[]), "runner-health", "exec")
    exec(compiled, namespace)
    feed.expiry = 200
    exec(compiled, namespace)
    assert not warnings
    assert [entry[0] for entry in pages] == [[(1, 100)], [(1, 200)]]
    assert all(entry[1]["auto_refresh_accounts"] == {1} for entry in pages)
    assert len(health) == feed.reads == 2
