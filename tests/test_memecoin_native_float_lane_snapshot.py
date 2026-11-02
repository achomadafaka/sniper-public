"""Flap NO_READ (2026-10-21, Gamma on fill c6c1ce7e): 6 of 17 bsc_flap_fast_lane fills since 10-07
kept a 1,240 ms WBNB unwrap because the native-float read had not finished when the executor
decided, ~50 ms after it started. A fresh fast-lane wallet snapshot now stands in for a pending
read; the 3-swap gas reserve and the book floor stay in the check, and a wrong-high snapshot meets
the existing insufficient-native fallback. Kill knob MEMECOIN_EVM_NATIVE_FLOAT_SNAPSHOT=0."""
import threading

import pytest

from sniper.execution import evm_native_float as F
from sniper.services import memecoin_bsc_fast_lane as bsc
from test_memecoin_bsc_early_native_float import _entry
from test_memecoin_bsc_flap_fast_handoff import EPOCH, _enabled, _payload  # noqa: F401
from test_memecoin_evm_native_float import AMOUNT, GAS_PRICE, LOW, RESERVE, SWAP_TX, UNWRAP, WETH, _c, _run

FLOOR = 10**15


def _pending_probe(snap, *, floor_fn=lambda: FLOOR, gas_price_fn=lambda: None):
    release = threading.Event()

    def read():
        release.wait(5)
        return None
    probe = F.NativeFloatProbe(read, quote_token=WETH, gas_price_fn=gas_price_fn, low_wei=LOW,
                               reserve_floor_wei=0, reserve_floor_fn=floor_fn, wait_for_read=False,
                               snapshot_fn=lambda: snap)
    return probe, release


def _settle(probe):
    for _ in range(200):
        if probe._floor_done or not probe._thread.is_alive():
            return
        threading.Event().wait(.005)


def test_a_pending_read_with_a_fresh_covering_snapshot_skips_the_unwrap():
    probe, release = _pending_probe({"native_wei": AMOUNT + RESERVE + FLOOR, "gas_price_wei": GAS_PRICE,
                                     "age_ms": 1200.0})
    _settle(probe)
    try:
        assert probe._thread.is_alive()
        assert probe.covers(UNWRAP, SWAP_TX) is True
        facts = probe.decision_facts
        assert facts["native_source"] == "lane_snapshot" and facts["snapshot_age_ms"] == 1200.0
        assert facts["gas_price_wei"] == GAS_PRICE and facts["native_read_ms"] is None
        assert facts["native_read_elapsed_ms"] >= 0
    finally:
        release.set()


@pytest.mark.parametrize("case", ["no_snapshot", "short", "zero_gas", "floor_pending", "raises"])
def test_a_pending_read_without_a_usable_snapshot_keeps_the_unwrap(case):
    covering = {"native_wei": AMOUNT + RESERVE + FLOOR, "gas_price_wei": GAS_PRICE}
    snap = {"no_snapshot": None, "short": dict(covering, native_wei=AMOUNT + RESERVE - 1),
            "zero_gas": dict(covering, gas_price_wei=0)}.get(case, covering)
    gate = threading.Event()

    def floor():
        if case == "floor_pending":
            gate.wait(5)
        if case == "raises":
            raise RuntimeError("book unreadable")
        return FLOOR
    probe, release = _pending_probe(snap, floor_fn=floor)
    if case != "floor_pending":
        _settle(probe)
    try:
        assert probe.covers(UNWRAP, SWAP_TX) is False
    finally:
        gate.set(), release.set()


def test_a_completed_read_wins_over_the_snapshot():
    probe = F.NativeFloatProbe(lambda: AMOUNT, quote_token=WETH, gas_price_fn=lambda: GAS_PRICE,
                               low_wei=LOW, wait_for_read=False,
                               snapshot_fn=lambda: {"native_wei": 10**20, "gas_price_wei": GAS_PRICE})
    probe._thread.join(1)
    assert probe.covers(UNWRAP, SWAP_TX) is False
    assert probe.decision_facts["native_source"] == "probe"
    assert probe.decision_facts["native_read_ms"] is not None


def test_a_wrong_high_snapshot_falls_back_to_the_unwrap_once():
    probe, release = _pending_probe({"native_wei": AMOUNT + RESERVE + FLOOR, "gas_price_wei": GAS_PRICE})
    _settle(probe)
    try:
        result, log = _run(_c(), send_results=[False, "0xhash42"], usdc=(100.0, 66.13), probe=probe)
    finally:
        release.set()
    assert result["status"] == "filled"
    assert log["released"] == [41] and log["pipelined"] == [UNWRAP]


FRESH = {"native_wei": 2 * 10**18, "gas_price_wei": 60_000_000, "age_ms": 500.0}


@pytest.mark.parametrize("snapshot,unwraps,source", [(lambda: FRESH, 0, "lane_snapshot"),
                                                     (lambda: None, 1, None)])
def test_no_read_at_decision_uses_only_a_fresh_lane_snapshot(monkeypatch, tmp_path, snapshot, unwraps, source):
    got = _entry(monkeypatch, tmp_path, real=True, pending=True, lane_snapshot=snapshot)
    assert got["result"]["status"] == "filled", got["result"]
    assert len([tx for tx in got["signed"] if tx.get("data", "").startswith("0x2e1a7d4d")]) == unwraps
    stage = next(f for n, f in got["stages"] if n == "evm.native_float")
    assert stage["outcome"] == ("unwrap_skipped" if unwraps == 0 else "unwrap_kept")
    assert stage.get("native_source") == source


def test_the_kill_knob_restores_todays_unwrap(monkeypatch, tmp_path):
    monkeypatch.setenv("MEMECOIN_EVM_NATIVE_FLOAT_SNAPSHOT", "0")
    got = _entry(monkeypatch, tmp_path, real=True, pending=True, lane_snapshot=lambda: FRESH)
    assert len([tx for tx in got["signed"] if tx.get("data", "").startswith("0x2e1a7d4d")]) == 1


def _live(snap, generation=EPOCH - 60):
    live = object.__new__(bsc.BscLive)
    live.snapshot = type("S", (), {"current": lambda self: dict(snap) if snap else None})()
    live.host_clock, live.generation_fn = (lambda: EPOCH), (lambda: generation)
    live.generation_margin_s, live.max_snapshot_age_s = 0.5, 30.0
    return live


@pytest.mark.parametrize("snap,generation,usable", [
    ({"native_wei": 1, "gas_price_wei": 1, "read_at": EPOCH - 3}, EPOCH - 60, True),
    ({"native_wei": 1, "gas_price_wei": 1, "read_at": EPOCH - 31}, EPOCH - 60, False),   # too old
    ({"native_wei": 1, "gas_price_wei": 1, "read_at": EPOCH - 3}, EPOCH - 3.2, False),   # predates a wallet change
    ({"native_wei": 1, "gas_price_wei": 1, "read_at": EPOCH + 1}, EPOCH - 60, False),    # future clock
    (None, EPOCH - 60, False)])
def test_the_lane_offers_only_a_snapshot_after_the_latest_wallet_change(snap, generation, usable):
    got = _live(snap, generation).float_snapshot()
    assert (got is not None) is usable
    if usable:
        assert got == {"native_wei": 1, "gas_price_wei": 1, "age_ms": 3000.0}


def test_the_lane_scopes_its_snapshot_to_the_hook_call_only(_enabled):
    seen = []

    class Cache:
        def stats(self, mint, now):
            return None

        def put_market_stats(self, *a):
            pass

    def factory(contexts, stats_fn):
        return lambda conn, ev: seen.append(F.LANE_SNAPSHOT.get()) or {"status": "rejected", "reason": "x"}
    live = _live({"native_wei": 1, "gas_price_wei": 1, "read_at": EPOCH - 1})
    live.ctx, live.contexts, live.conn, live.cache = {}, {"56": {}}, None, Cache()
    live.v3_hook_factory, live.quotes, live.stats_fn, live.stopped = factory, {}, None, False
    live.host_clock = lambda: EPOCH + .1
    live.handle(_payload(), recv_epoch=EPOCH + .2)
    assert seen[0][0] == "56" and seen[0][1] == live.float_snapshot
    assert F.LANE_SNAPSHOT.get() is None
