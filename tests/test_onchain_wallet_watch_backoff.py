"""The Solana watcher's reconnect backoff and its dark-watcher heartbeat (2026-09-28).

Every error used to double the wait up to 300 s. On 2026-09-28 a keepalive timeout after a relaunch,
then six empty-error connect failures (2, 4, ..., 128 s) left the watcher dark 21:17:42-21:26:25Z,
and the heartbeat kept its last subs=33, so nothing could see it. Now only a quota answer earns the
long wait, and a backing-off watcher writes connected=false, subs=0.

No network: the socket is faked and asyncio.sleep is recorded.
"""
import asyncio
import json

from test_onchain_wallet_watch_registry_refresh import A, _FakeSocket, _load


def test_other_errors_retry_within_eight_seconds(tmp_path):
    w = _load(tmp_path)
    state = w.fresh_backoff()
    assert [w.reconnect_delay(state, "") for _ in range(6)] == [2.0, 4.0, 8.0, 8.0, 8.0, 8.0]


def test_a_quota_answer_still_backs_off_hard(tmp_path):
    w = _load(tmp_path)
    state = w.fresh_backoff()
    got = [w.reconnect_delay(state, "server rejected WebSocket connection: HTTP 429")
           for _ in range(10)]
    assert got == [2.0, 4.0, 8.0, 16.0, 32.0, 64.0, 128.0, 256.0, 300.0, 300.0]


def test_after_a_quota_answer_no_error_retries_faster_than_its_floor(tmp_path):
    """The 2026-08-21 burn was a fast loop against a 429: a timeout between two 429s must not
    bring the retry back to 2 s."""
    w = _load(tmp_path)
    state = w.fresh_backoff()
    for _ in range(5):
        w.reconnect_delay(state, "usage cap reached")               # floor 32 s
    assert w.reconnect_delay(state, "") == 32.0
    assert w.reconnect_delay(state, "keepalive ping timeout") == 32.0


def test_a_dark_watcher_beats_disconnected_then_reconnects(tmp_path, monkeypatch):
    w = _load(tmp_path)
    w.DB = tmp_path / "detections.db"
    sock = _FakeSocket()
    failures = [TimeoutError(), TimeoutError(), TimeoutError()]

    def connect(*a, **k):
        if failures:
            raise failures.pop(0)
        return sock
    monkeypatch.setattr(w.websockets, "connect", connect)
    monkeypatch.setattr(w, "desired_watchlist", lambda: (
        {A: {"uid": "u-a", "handle": "a", "status": "registry_verified"}}, 0))

    async def record(sig, addr, info, wdb, frame_mono=None, *, sol_usd=None):
        return None
    monkeypatch.setattr(w, "process_signature", record)
    waits, beats = [], []
    real_sleep = asyncio.sleep

    async def fake_sleep(delay, *a, **k):
        if delay >= 1.0:                       # the backoff; the fake socket idles at 0.005 s
            waits.append(delay)
            beats.append(json.loads(w.HEARTBEAT.read_text()))
            return await real_sleep(0)
        return await real_sleep(delay)
    monkeypatch.setattr(w.asyncio, "sleep", fake_sleep)

    asyncio.run(w._run(0.3, None, {}))

    assert waits == [2.0, 4.0, 8.0]
    assert all(b["connected"] is False and b["subs"] == 0 for b in beats), beats
    assert list(beats[0])[:2] == ["ts", "subs"], "the watchdog's sed reads the first plain subs"
    assert beats[0]["disconnected_since"] == beats[-1]["disconnected_since"], "one outage, one start"
    after = json.loads(w.HEARTBEAT.read_text())
    assert after["connected"] is True and after["subs"] == 1
