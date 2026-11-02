"""The rabbit tape volume probe (scripts/research/rabbit_tape_probe.py): the plan never connects, the
subscription asks for exactly the two processed filters, and the decoders read real field names."""
import importlib.util
import json
import struct
from pathlib import Path

from sniper.feeds.yellowstone import geyser_pb2 as pb

spec = importlib.util.spec_from_file_location(
    "rabbit_tape_probe", Path(__file__).resolve().parents[1] / "scripts/research/rabbit_tape_probe.py")
P = importlib.util.module_from_spec(spec)
spec.loader.exec_module(P)


def test_without_connect_it_prints_the_plan_and_never_opens_a_channel(monkeypatch, capsys):
    import grpc
    monkeypatch.setattr(grpc.aio, "secure_channel", lambda *a, **k: (_ for _ in ()).throw(AssertionError("connected")))
    assert P.main([]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["connected"] is False and out["commitment"] == "processed"


def test_the_subscription_is_processed_curve_accounts_and_successful_amm_transactions():
    req = P.subscription()
    assert req.commitment == pb.CommitmentLevel.PROCESSED
    assert list(req.accounts["curve"].owner) == [P.PUMP_PROGRAM] and not req.accounts["curve"].account
    amm = req.transactions["amm"]
    assert list(amm.account_include) == [P.PUMP_AMM_PROGRAM] and amm.failed is False and amm.vote is False


def test_the_decoders_read_a_curve_write_and_a_pumpswap_transaction():
    u = pb.SubscribeUpdate()
    u.account.slot = 7
    u.account.account.pubkey = bytes(32)
    u.account.account.write_version = 9
    u.account.account.data = P.CURVE_DISCRIMINATOR + struct.pack("<5Q", 1, 2, 3, 4, 5) + b"\x00" + bytes(40)
    assert P.decode_curve(u)[1:] == (7, 9, 1, 2, 3, 4, 0)
    short = pb.SubscribeUpdate()
    short.account.account.data = bytes(10)
    assert P.decode_curve(short) is None
    t = pb.SubscribeUpdate()
    t.transaction.slot = 11
    t.transaction.transaction.index = 3
    b = t.transaction.transaction.meta.post_token_balances.add()
    b.account_index, b.mint, b.ui_token_amount.amount = 2, "M", "100"
    assert P.decode_amm(t) == (11, 3, [(2, "M", "100")])


def test_a_duration_past_thirty_minutes_is_refused():
    import pytest
    with pytest.raises(SystemExit):
        P.main(["--duration", "3600"])


def test_a_pump_account_without_the_curve_discriminator_is_skipped():
    """Gamma LOW-1: Global, fee config and other pump-owned accounts are not curves."""
    u = pb.SubscribeUpdate()
    u.account.account.data = bytes(8) + struct.pack("<5Q", 1, 2, 3, 4, 5) + bytes(41)
    assert P.decode_curve(u) is None
    m = P.Meter()
    m.add(u, P.decode_curve, 1)
    assert m.skipped == 1 and m.decoded == 0


def test_a_stalled_stream_ends_the_run_on_time():
    """Gamma LOW-2: no frame ever arrives; the wait is bounded by the time left."""
    import asyncio
    import time as _t

    class Silent:
        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.sleep(3600)

    t0 = _t.monotonic()
    curve, amm, counts = asyncio.run(P.consume(Silent(), 0.2))
    assert _t.monotonic() - t0 < 2.0 and counts["ended"] == "duration" and curve.frames == amm.frames == 0


# --- the run guard (Alpha, after the 11:07:56Z deploy abort) ---------------------------------------

def _hb(path, *, ts, started, feeds=0, preconf=0):
    path.write_text(json.dumps({"ts": ts, "started_at": started, "slot_lane": {
        "reconnects": 0, "signal_feeds": {"shyft:fra": {"reconnects": feeds}},
        "preconf": {"feed": {"reconnects": preconf}}, "preconf_sources": {"bam": {"feed": {"reconnects": 0}}}}}))


def _guard(tmp_path, now):
    path = tmp_path / "hb"
    return path, P.HeartbeatGuard(path, clock=lambda: now["t"])


def test_a_deploy_restart_is_logged_and_the_probe_keeps_running(tmp_path):
    now = {"t": 1000.0}
    path, g = _guard(tmp_path, now)
    _hb(path, ts=1000.0, started="old", feeds=3)
    assert g.check() is None
    now["t"] = 1008.0                                  # the old runner stopped: stale
    assert g.check() is None
    _hb(path, ts=1012.0, started="new", feeds=0)       # a new runner, its counters from zero
    now["t"] = 1012.5
    assert g.check() is None and g.events[0]["event"] == "deploy_restart" and g.events[0]["gap_s"] == 4.5
    _hb(path, ts=1013.0, started="new", feeds=1)       # a reconnect under the NEW runner still aborts
    now["t"] = 1013.2
    assert g.check() == "feed_reconnect:feed:shyft:fra"


def test_a_gap_under_the_same_runner_aborts_and_so_does_a_long_one(tmp_path):
    now = {"t": 1000.0}
    path, g = _guard(tmp_path, now)
    _hb(path, ts=1000.0, started="r")
    g.check()
    now["t"] = 1006.0
    assert g.check() is None                           # stale: wait to see which runner returns
    _hb(path, ts=1006.5, started="r")
    now["t"] = 1006.6
    assert g.check() == "heartbeat_gap_same_runner"
    now2 = {"t": 2000.0}
    path2, g2 = _guard(tmp_path / ".." / tmp_path.name, now2)
    _hb(path2, ts=2000.0, started="r")
    g2.check()
    now2["t"] = 2000.0 + 6 + P.HeartbeatGuard.RESTART_GRACE_S
    assert g2.check() is None and g2.gap_since is not None
    now2["t"] += P.HeartbeatGuard.RESTART_GRACE_S + 1
    assert g2.check() == "heartbeat_stale"


def test_a_preconf_reconnect_aborts(tmp_path):
    now = {"t": 1000.0}
    path, g = _guard(tmp_path, now)
    _hb(path, ts=1000.0, started="r")
    g.check()
    _hb(path, ts=1001.0, started="r", preconf=1)
    now["t"] = 1001.1
    assert g.check() == "feed_reconnect:preconf"


def test_a_stop_ends_the_run_with_its_reason_and_keeps_the_meters():
    import asyncio

    async def run():
        stop, why = asyncio.Event(), {}

        class Stream:
            n = 0
            def __aiter__(self):
                return self
            async def __anext__(self):
                self.n += 1
                if self.n == 3:
                    why["reason"] = "sigterm"
                    stop.set()
                    await asyncio.sleep(3600)
                u = pb.SubscribeUpdate()
                u.slot.slot = self.n
                return u
        return await P.consume(Stream(), 30.0, stop=stop, why=why)
    curve, amm, counts = asyncio.run(run())
    assert counts["ended"] == "sigterm" and counts["slot_frames"] == 2


def test_a_stream_error_mid_run_keeps_the_meters_up_to_it():
    import asyncio

    class Stream:
        n = 0
        def __aiter__(self):
            return self
        async def __anext__(self):
            self.n += 1
            if self.n == 3:
                raise RuntimeError("RESOURCE_EXHAUSTED: too many connections")
            u = pb.SubscribeUpdate()
            u.pong.id = self.n
            return u
    curve, amm, counts = asyncio.run(P.consume(Stream(), 30.0))
    assert counts["ended"] == "error" and counts["pongs"] == 2 and "RESOURCE_EXHAUSTED" in counts["first_error"]


def test_sigterm_mid_probe_prints_partial_meters_with_end_reason_sigterm(monkeypatch, tmp_path):
    """End to end through probe(): the fake channel streams pongs; SIGTERM lands mid-run."""
    import asyncio
    import os
    import signal
    import grpc

    class Call:
        def __init__(self, requests):
            self.n = 0
        def __aiter__(self):
            return self
        async def __anext__(self):
            self.n += 1
            if self.n == 3:
                os.kill(os.getpid(), signal.SIGTERM)
            await asyncio.sleep(0.01)
            u = pb.SubscribeUpdate()
            u.pong.id = self.n
            return u
        def cancel(self):
            pass

    class Channel:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        def stream_stream(self, *a, **k):
            return lambda requests, metadata=None: Call(requests)
    monkeypatch.setattr(grpc.aio, "secure_channel", lambda *a, **k: Channel())
    hb = tmp_path / "hb"
    _hb(hb, ts=__import__("time").time(), started="r")
    out = asyncio.run(P.probe("https://grpc.fra.shyft.to", "t", 30.0, guard=P.HeartbeatGuard(hb)))
    assert out["ended"] == "sigterm" and out["pongs"] >= 2 and out["guard_events"] == []


def test_connect_refuses_without_a_readable_heartbeat(tmp_path, capsys, monkeypatch):
    import grpc
    monkeypatch.setattr(grpc.aio, "secure_channel", lambda *a, **k: (_ for _ in ()).throw(AssertionError("connected")))
    assert P.main(["--connect", "--heartbeat", str(tmp_path / "missing")]) == 2
    assert "heartbeat not found" in capsys.readouterr().out


def test_one_stop_waiter_serves_the_whole_run_not_one_per_frame():
    """Gamma LOW on 4047277a6: per-frame stop waiters inflate process_cpu_pct, the headline number."""
    import asyncio

    class CountingEvent(asyncio.Event):
        waits = 0
        async def wait(self):
            CountingEvent.waits += 1
            return await super().wait()

    class Stream:
        n = 0
        def __aiter__(self):
            return self
        async def __anext__(self):
            self.n += 1
            if self.n > 50:
                raise StopAsyncIteration
            u = pb.SubscribeUpdate()
            u.pong.id = self.n
            return u

    async def run():
        return await P.consume(Stream(), 30.0, stop=CountingEvent())
    curve, amm, counts = asyncio.run(run())
    assert counts["pongs"] == 50 and counts["ended"] == "stream_end" and CountingEvent.waits == 1
