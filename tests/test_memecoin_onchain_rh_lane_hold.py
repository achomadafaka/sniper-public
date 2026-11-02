"""4663 fast-lane misses C (73809, 2026-10-14): the on-chain lane never decides a buy the live rh
lane is still deciding. The on-chain attempt began at .852, 8 ms after the lane had the datagram
(.844) and before its hook refused the buy over the ceiling (.980); the on-chain gate then priced the
coin from 1B-implied feed readings and filled it."""
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from scripts.research.onchain_wallet_watch import SCHEMA
from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_onchain_lane as lane
from sniper.services import memecoin_rh_fast_lane as rh

NOW = datetime(2026, 10, 13, 6, 42, 44, 900000, tzinfo=timezone.utc)
ARRIVAL = (NOW - timedelta(seconds=0.1)).isoformat()
TX = "0x1560d7dba9dd8be65c6adfbede2f81b031cee245067f6fc79a2175d1f8885f0f"
MINT = "0xdd90bfa4adc7f4401e611abac692d939f9f4cb07"
UID = "0xdetweiler"
SIGNATURE = f"4663:{TX}:{MINT}"


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


@pytest.fixture
def ledger():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    m.ensure_memecoin_tables(conn)
    conn.execute("INSERT INTO onchain_wallet_detections(signature,source_user_id,handle,address,"
                 "network_id,mint,source_slot,source_size_usd,source_price,detected_at,created_at,"
                 "source_valuation_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                 (SIGNATURE, UID, UID, "0x" + "83" * 20, "4663", MINT, 16, 7478.6,
                  7.97957543258506e-06, ARRIVAL, ARRIVAL, json.dumps({})))
    yield conn
    conn.close()


@pytest.fixture
def holds(monkeypatch):
    clock, wakes = Clock(), []
    got = rh.LaneHolds(bound_s=1.0, clock=clock, wake_fn=lambda: wakes.append(clock.t))
    monkeypatch.setattr(rh, "LANE_HOLDS", got)
    return got, clock, wakes


def sweep(conn, hook, *, cursor=0, deferred=None):
    return lane.sweep_onchain_detections(conn, hook, cursor=cursor,
        now_fn=lambda: NOW.isoformat(), max_age_s=45, network_id="4663", deferred=deferred,
        approved_ids_fn=lambda _conn, chain: {UID} if chain == "4663" else set())


def test_onchain_lane_waits_while_the_rh_lane_holds_the_same_buy(ledger, holds):
    held, clock, wakes = holds
    calls, deferred = [], {}
    hook = lambda c, ev: calls.append(ev) or {"status": "rejected", "reason": "ordinary refusal"}
    held.hold(TX.upper().replace("0X", "0x"), MINT)
    cursor, results = sweep(ledger, hook, deferred=deferred)
    assert calls == [], "the on-chain lane decided while the rh lane held the buy"
    assert results[0]["result"] == {"status": "deferred", "reason": "rh_fast_lane_in_flight"}
    clock.t += 0.5
    cursor, results = sweep(ledger, hook, cursor=cursor, deferred=deferred)
    assert calls == [] and results == []
    held.release(TX, MINT)                       # the lane's verdict
    assert wakes == [100.5], "a release the on-chain lane waited on must wake it"
    cursor, results = sweep(ledger, hook, cursor=cursor, deferred=deferred)
    assert len(calls) == 1 and calls[0].fomo_event_id == "onchain:" + SIGNATURE
    assert not deferred


def test_the_hold_ends_at_its_bound_without_a_verdict(ledger, holds):
    held, clock, wakes = holds
    calls, deferred = [], {}
    hook = lambda c, ev: calls.append(ev) or {"status": "filled"}
    held.hold(TX, MINT)
    cursor, _ = sweep(ledger, hook, deferred=deferred)
    clock.t += 1.0                               # a crashed or stuck lane never releases
    sweep(ledger, hook, cursor=cursor, deferred=deferred)
    assert len(calls) == 1 and not deferred and wakes == []


@pytest.mark.parametrize("other", ["tx", "mint"])
def test_another_buy_is_never_held(ledger, holds, other):
    held, _clock, _wakes = holds
    held.hold("0x" + "99" * 32 if other == "tx" else TX, MINT if other == "tx" else "0x" + "99" * 20)
    calls = []
    sweep(ledger, lambda c, ev: calls.append(ev) or {"status": "filled"}, deferred={})
    assert len(calls) == 1


def test_a_release_nobody_waited_on_sends_no_wake(holds):
    held, _clock, wakes = holds
    held.hold(TX, MINT)
    held.release(TX, MINT)
    assert wakes == [] and not held.holds(TX, MINT)


@pytest.mark.parametrize("reason", ["hook:skipped_mcap:mcap $831,550 over ceiling", "shared_queued"])
def test_the_live_lane_holds_from_datagram_receipt_until_its_verdict(holds, monkeypatch, reason):
    held, _clock, _wakes = holds
    monkeypatch.setattr(rh, "check_sender", lambda *a, **k: None)
    monkeypatch.setattr(rh, "detector_pid", lambda *a, **k: 1)
    monkeypatch.setattr(rh, "write_record", lambda *a, **k: None)
    monkeypatch.setattr(rh, "write_live_record", lambda *a, **k: None)
    monkeypatch.setattr(rh, "route_kind", lambda p: "v4_shared" if reason == "shared_queued" else "v4_native")
    seen_during = []

    def handle(payload, **_kw):
        seen_during.append(held.holds(TX, MINT))
        return {"reason": reason}

    cache = SimpleNamespace(note_candidates=lambda *a, **k: None, reason=lambda *a, **k: None,
                            stats=lambda *a, **k: None)
    dispatch = SimpleNamespace(beat=None, drops=set(), seen=SimpleNamespace(first=lambda p: True),
                               counts={"signals": 0}, live=SimpleNamespace(handle=handle),
                               registry={}, cache=cache, host_clock=lambda: 1.0, db_path=None)
    dispatch._on_signal = lambda payload, recv_epoch: rh.LaneDispatch._on_signal(
        dispatch, payload, recv_epoch)
    payload = {"kind": "signal", "tx_hash": TX, "mint": MINT, "key": {}, "pool": None}
    rh.LaneDispatch._on_datagram(dispatch, payload, 1, 1, 1.0)
    assert seen_during == [True]
    # A queued shared route decides on the shared worker, which releases the hold itself.
    assert held.holds(TX, MINT) is (reason == "shared_queued")
