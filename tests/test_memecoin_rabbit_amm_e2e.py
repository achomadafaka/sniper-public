"""The OKX pre-execution handler, end to end from real signed frame bytes (Gamma HIGH-1/2 on
2906fa4df, Alpha 2026-10-17): wire bytes -> the preconf frame sink -> preparation_candidate (which
must find a preparation target) -> the shared RabbitBuyer with the handlers registered as the signal
loop registers them -> one read -> a fire that passes the pre-execution proof gate. Then the signal
loop itself registers the OKX handler on the lane's sol_usd reader, and the heartbeat exports each
handler's status().

Fixture: the `raw_b64` wire bytes in okx_pump_amm_frames.json (v1 frames; a v0 frame with lookup
tables has no metadata before execution and stays frame-log only). DFlow's own end-to-end test is
Lambda's, in test_memecoin_preconf_signal.
"""
import asyncio
import base64
import json
import threading
import time
from pathlib import Path

import pytest

from sniper.services import memecoin_preconf_signal as P
from sniper.services import memecoin_rabbit_signal as R
from sniper.services import memecoin_shyft_prepare as prep
from sniper.services.memecoin_feed_arrivals import StampedArrivals
from sniper.services.memecoin_rabbit_dflow_amm import DflowAmmHandler
from sniper.services.memecoin_rabbit_okx_amm import OkxAmmHandler, sol_usd_reader
from sniper.services.memecoin_source_initiation import source_initiation_refusal
from sniper.settings import memecoin as cfg
from test_memecoin_preconf_signal import _WS, _frame
from test_memecoin_rabbit_dflow_amm import candidate
from test_memecoin_rabbit_signal_stage2 import _ev, backstop
import test_memecoin_rabbit_okx_amm as okx_t

OKX = json.loads((Path(__file__).parent / "fixtures/memecoin/okx_pump_amm_frames.json").read_text())


@pytest.fixture
def knobs(monkeypatch):
    for knob in ("MEMECOIN_SLOT_LANE_RABBIT_SIGNAL", "MEMECOIN_SLOT_LANE_RABBIT_BUY", "MEMECOIN_SLOT_LANE_PRECONF",
                 "MEMECOIN_SLOT_LANE_RABBIT_DFLOW_AMM", "MEMECOIN_SLOT_LANE_RABBIT_OKX_AMM"):
        monkeypatch.setattr(cfg, knob, True)


def _case():
    """(json tx, wire bytes, the read's accounts, sol_usd) for the first OKX v1 frame with bytes."""
    sig = sorted(OKX["raw_b64"])[0]
    tx = next(t for t in OKX["pure"].values() if t["transaction"]["signatures"][0] == sig)
    price = OKX["sol_usd"][sig]
    plan, why = OkxAmmHandler(read_fn=None, sol_usd_fn=lambda: (price, 1.0)).match(candidate(tx))
    assert why is None, why
    return tx, base64.b64decode(OKX["raw_b64"][sig]), okx_t.state_for(tx, plan, read_slot=tx["slot"] - 1), price


def _read(state):
    def read(method, params):
        assert method == "getMultipleAccounts"
        return {"context": {"slot": state["slot"]}, "value": [state["accounts"].get(k) for k in params[0]]}
    read.ping = lambda: None
    return read


def _buyer(read, owner, price):
    """The shared buyer with the handlers in the signal loop's order (curve, DFlow, OKX)."""
    stats, fired = {}, []
    rabbit = backstop(read)
    landing = R.LandingWatch(backstop=rabbit, head_fn=lambda: (None, None), status={})
    buyer = R.RabbitBuyer(seen={}, owners_fn=lambda: {owner: ("uid", "h")}, on_buy=fired.append,
                          backstop=rabbit, feed=P.FEED, status=stats, landing=landing,
                          head_fn=lambda: (None, None), epoch_fn=lambda: 1760000000.0)
    buyer.handlers += [DflowAmmHandler(read_fn=buyer.read),
                       OkxAmmHandler(read_fn=buyer.read, sol_usd_fn=lambda: (price or 100.0, 1.0))]
    return buyer, stats, fired


def test_a_real_okx_frame_fires_end_to_end_through_the_preparation_gate(knobs):
    route = "okx_pump_amm"
    tx, raw, state, price = _case()
    owner = candidate(tx)["owner"]
    update, _verify = P.frame_update(tx["slot"], raw)
    cand = prep.preparation_candidate(update, {owner: ("uid", "h")}, recv_monotonic=1.0, generation=1)
    assert cand is not None and prep.preparation_target(cand)["venue"] == "pump_amm"
    buyer, stats, fired = _buyer(_read(state), owner, price)
    status, notes = {}, []
    frames = type("Frames", (), {"frame": lambda self, *a: None,
                                 "note": lambda self, sig, own, verdict=None, **kw: notes.append(verdict)})()
    sink = P.FrameSink(feed=P.FEED, owners_fn=lambda: {owner: ("uid", "h")},
                       arrivals=StampedArrivals(("shyft:grpc.fra.shyft.to", P.FEED)),
                       frames=frames, status=status, buyer=buyer)

    async def main():
        sink(tx["slot"], P.STATUS_SUCCESS, raw, time.monotonic(), 1)
        while buyer._tasks:
            await asyncio.sleep(0)
            await asyncio.gather(*list(buyer._tasks), return_exceptions=True)
    asyncio.run(main())
    assert status["offered"] == 1 and stats["fired"] == 1 and len(fired) == 1, (status, stats)
    buy = fired[0]
    assert buy.rabbit["route"] == route and buy.venue == "pump_amm" and buy.signal_feed == P.FEED
    assert buy.source_initiation["feed"] == P.FEED and buy.source_initiation["signature_basis"] == "ed25519_local"
    assert source_initiation_refusal(_ev(buy), "solana") is None
    executed = okx_t.executed_event(tx)
    assert buy.pool == executed["pool"]
    assert abs(buy.token_amount - int(executed["base_amount_out"])) * 10_000 <= 20 * int(executed["base_amount_out"])


def test_the_signal_loop_registers_the_okx_handler_on_the_lane_reader_and_the_heartbeat_reports_it(
        knobs, monkeypatch, tmp_path):
    from sniper.services import memecoin_runner as runner
    from sniper.services import memecoin_shyft_signal as full
    from sniper.services import memecoin_slot_signal as signal
    tx, raw, state, price = _case()
    owner = candidate(tx)["owner"]
    status, fired, stop, sent = {}, [], threading.Event(), []
    rabbit = backstop(_read(state))

    async def idle(*a, **k):
        while not stop.is_set():
            await asyncio.sleep(0.01)
    monkeypatch.setattr(prep, "run_prepare_feed", idle)
    monkeypatch.setattr(full, "run_feed", idle)

    def on_buy(buy):
        fired.append(buy)
        stop.set()

    def connect(url):
        return _WS([json.dumps({"jsonrpc": "2.0", "id": 1, "result": 5}), _frame(tx["slot"], raw)], sent)
    asyncio.run(asyncio.wait_for(signal._run_signals(
        on_buy, lambda: {owner: ("uid", "h")}, None, status, stop, None, None, None, None,
        ("token", ("https://grpc.ams.shyft.to",)), infra_provider="shyft",
        prepare_config=("token", "https://rabbitstream.fra.shyft.to"), on_prepare=lambda c: None,
        rabbit=rabbit, preconf={"url": "wss://beta.helius-rpc.com/?api-key=K", "connect": connect,
                                "alert_fn": lambda *a: None, "credit_path": tmp_path / "credits.json"},
        rabbit_sol_usd_fn=lambda: (price, 2.0)), 10))
    assert status["rabbit_buy"]["handlers"] == ["pump_curve", "dflow_pump_amm", "okx_pump_amm", "jupiter_pump_amm"]
    assert len(fired) == 1 and fired[0].rabbit["route"] == "okx_pump_amm" and fired[0].rabbit["sol_usd"] == price
    path = tmp_path / "heartbeat.json"
    runner.write_heartbeat({}, path=path, auth_ok=True,
                           slot_lane_status={"mode": "live", "signal": status, "rabbit_breaker": rabbit.status})
    lane = json.loads(path.read_text())["slot_lane"]["rabbit_buy"]
    assert lane["handler_status"]["okx_pump_amm"]["fills"] == 1 and lane["handler_status"]["okx_pump_amm"]["matched"] == 1
    assert lane["handler_status"]["okx_pump_amm"]["spend_drift_bps"] == {"n": 0, "p50": None, "p90": None}
    assert "amm_unwired" not in lane


def test_the_lane_hands_the_signal_thread_a_late_sol_usd_reader(monkeypatch):
    """The prefetcher is built after the signal thread starts: until then the reader raises (the OKX
    handler refuses as sol_usd_stale); after, it reads the prefetcher's snapshot and its age."""
    from sniper.execution import pumpswap_buy as ps
    from sniper.services import memecoin_real_runner as rr
    from sniper.services import memecoin_slot_lane as lane
    from test_slot_lane_pool_prewarm import _Rpc

    class Thread:
        def __init__(self, *, target, name, daemon):
            pass

        def start(self):
            pass
    monkeypatch.setattr("threading.Thread", Thread)
    monkeypatch.setattr(rr, "_momentum_read_pool", lambda: None)
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_MISS_LEARN", False)
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_RABBIT_OKX_AMM", True)
    got, early = {}, []

    def signal_start_fn(on_buy, **kw):
        got.update(kw)
        try:
            early.append(kw["rabbit_sol_usd_fn"]())
        except KeyError:
            early.append("not yet")
        handler = OkxAmmHandler(read_fn=None, sol_usd_fn=kw["rabbit_sol_usd_fn"], alert_fn=lambda *a: None)
        early.append(handler.match(candidate(OKX["pure"][sorted(OKX["pure"])[0]])))
    amm = type("Amm", (), {"pools": ps.PumpSwapPoolCache(rpc=_Rpc({}))})()
    rc = dict(usdc_fn=lambda: None, sol_fn=lambda: None, latest_blockhash_fn=lambda: None, slot_lock_key="fixture")
    lane.start_slot_lane(rc, builders={"pump_amm": amm}, signal_start_fn=signal_start_fn,
                         sol_usd_fn=lambda: None, log=lambda _: None, roster_path="unused",
                         conn_factory=object, readonly_conn_factory=object)
    assert early == ["not yet", (None, "sol_usd_stale")]
    assert got["rabbit_sol_usd_fn"]() == (None, None)          # bound now: the prefetcher, never refreshed
    assert sol_usd_reader(lambda: None)() == (None, None)


def test_start_signal_thread_hands_the_reader_to_the_signal_loop(monkeypatch):
    from sniper.services import memecoin_slot_signal as signal
    got = {}

    async def run_signals(*a, **kw):
        got.update(kw)
    monkeypatch.setattr(signal, "_run_signals", run_signals)
    monkeypatch.setattr(signal, "_shares_funded_key", lambda url: False)
    reader = lambda: (150.0, 1.0)                                    # noqa: E731
    thread = signal.start_signal_thread(lambda b: None, owners_fn=dict, ws_url="wss://signal.example",
                                        status={}, rabbit_sol_usd_fn=reader)
    thread.join(5)
    assert got["rabbit_sol_usd_fn"] is reader
