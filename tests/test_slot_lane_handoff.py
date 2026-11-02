"""A slot-lane refusal the ordinary on-chain lane can still judge is handed to it, never dropped.

Tekkerrss bought 54bzG3hj... on PumpSwap at 23:12:00Z on 2026-09-26 (S=450818643, $489 at ~$21k
mcap, inside the source floor and the ceiling). The slot lane refused it `pool_unknown` (its pool
cache had not learned the pool yet: miss-then-learn), J7 does not relay this wallet and the
confirmed watcher did not cover it, so no lane made an attempt. untaxxable's buy of 2FLwcVAh... at
22:54:31Z was refused `slot_lane_price_unreadable` (a curve not quoted in SOL: the event's SOL
fields are 0) and the watcher, which classifies SOL/USDC spends only, wrote nothing either.

Both frames are real: tests/fixtures/memecoin/slot_handoff_54bzG3hj.json (getTransaction at
confirmed, with the Tekkerrss pool's account and mint accounts as the pool cache's learn reads them).
"""
import contextlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from sniper.execution import pumpswap_buy as ps
from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_onchain_lane as onchain
from sniper.services import memecoin_slot_lane as lane
from sniper.services import memecoin_slot_signal as sig

FX = json.loads((Path(__file__).parent / "fixtures" / "memecoin" / "slot_handoff_54bzG3hj.json")
                .read_text())
RECV = "2026-09-26T23:12:00.041183+00:00"
SOL_USD = 120.0
MINT_54 = "54bzG3hjWCwL6UoeobcPnrXK5qUjxHn9USpfDY1Zpump"
MINT_2F = "2FLwcVAhQrmtha1pKM5jjHiC1cBbq3Vze3oBLJoypump"


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def perf_counter(self):
        return self.now


def _buy(entry, clock):
    return sig.decode_owner_buy(entry["frame"], entry["owner"], sig._owner_bytes(entry["owner"]),
                                entry["source_user_id"], entry["handle"],
                                recv_monotonic=clock.now, recv_utc=RECV)


class _PoolRpc:
    """Answers the pool cache's two learn reads with the fixture's accounts."""

    def __init__(self, pool_account=FX["tekkerrss"]["pool_account"]):
        self.pool_account = pool_account

    def __call__(self, method, params):
        if method == "getAccountInfo":
            return {"value": self.pool_account}
        if method == "getMultipleAccounts":
            return {"value": FX["tekkerrss"]["mint_accounts"]}
        raise AssertionError(method)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "ledger.db"
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    m.ensure_memecoin_tables(c)
    c.commit()
    with contextlib.closing(c):
        yield SimpleNamespace(conn=c, path=path)


def _factory(path):
    def open_conn():
        c = sqlite3.connect(path, check_same_thread=False)
        c.row_factory = sqlite3.Row
        return c
    return open_conn


def _snap(clock):
    seen = {"sol_usd": clock.now, "balances": clock.now, "blockhash": clock.now}
    return lane.PrefetchSnapshot(sol_usd=SOL_USD, usdc_balance=400.0, sol_balance=0.5,
                                 blockhash="Hash1", last_valid_height=1, observed_mono=seen)


def _lane(db, clock, pools, handoff, *, mode="live", armed=None):
    def forbidden(*_a, **_k):
        raise AssertionError("a refused buy never builds or sends")
    armed = armed or {FX["tekkerrss"]["source_user_id"], FX["untaxxable"]["source_user_id"]}
    return lane.SlotLane(conn=db.conn, executor={"slot_execute_fn": forbidden}, build_fn=forbidden,
                         snapshot_fn=lambda: _snap(clock), armed_ids_fn=lambda: armed,
                         kill_tripped_fn=lambda: False, now_iso_fn=lambda: RECV,
                         lock_key="sol-test", mode=mode, amm_pools=pools, clock=clock,
                         handoff=handoff)


def _detections(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM onchain_wallet_detections ORDER BY id")]


def _setup(db, *, rpc=None, mode="live"):
    clock = _Clock()
    pools = ps.PumpSwapPoolCache(rpc=rpc or _PoolRpc())
    handoff = lane.Handoff(pools=pools, conn_factory=_factory(db.path), max_age_s=45.0, clock=clock)
    return clock, pools, handoff, _lane(db, clock, pools, handoff, mode=mode)


def test_the_54bzG3hj_miss_reaches_the_onchain_lane_once_its_pool_is_learned(db):
    clock, pools, handoff, ln = _setup(db)
    buy = _buy(FX["tekkerrss"], clock)
    assert ln.handle(buy)["reason"] == "pool_unknown"
    clock.now += 0.3
    assert handoff.flush() == []                     # the pool is still queued for its learn
    assert _detections(db.conn) == []
    pools.learn_pending()                            # the prefetch thread's refresh, same tick
    clock.now += 0.2
    [done] = handoff.flush()
    assert done["outcome"] == "inserted" and done["mint"] == MINT_54
    [row] = _detections(db.conn)
    ev = ps.parse_buy_event(buy.trade_event_log)
    paid = ps.buy_event_gross_quote(ev)            # the lanes' one source-size basis (2026-09-28)
    reason, facts = lane.amm_market_facts(buy, pools, SOL_USD)
    assert reason is None
    assert row["signature"] == FX["tekkerrss"]["frame"]["params"]["result"]["value"]["signature"]
    assert (row["source_user_id"], row["handle"], row["address"], row["network_id"], row["mint"]) == (
        FX["tekkerrss"]["source_user_id"], "Tekkerrss", FX["tekkerrss"]["owner"], "solana", MINT_54)
    assert row["source_size_usd"] == pytest.approx(paid / 1e9 * SOL_USD)
    assert row["source_size_usd"] == pytest.approx(491.4, abs=0.1)     # 4.095 SOL paid at $120
    assert row["source_price"] == pytest.approx(facts["price_usd"])
    assert row["source_slot"] == buy.slot
    assert row["detected_at"] == RECV
    valuation = json.loads(row["source_valuation_json"])
    assert valuation["basis"] == lane.HANDOFF_BASIS and valuation["reason"] == "pool_unknown"
    assert valuation["source_slot"] == 450_818_643 and valuation["sol_usd"] == SOL_USD

    seen = []

    def hook(_conn, ev_):
        seen.append(ev_)
        return {"status": "rejected", "reason": "test"}
    _, results = onchain.sweep_onchain_detections(
        db.conn, hook, cursor=0, now_fn=lambda: "2026-09-26T23:12:02+00:00", max_age_s=45.0,
        approved_ids_fn=lambda _c, _n: {FX["tekkerrss"]["source_user_id"]}, deferred={})
    assert [r["result"]["status"] for r in results] == ["rejected"]
    [ev_] = seen
    assert (ev_.mint, ev_.fomo_event_id) == (MINT_54, "onchain:" + row["signature"])
    assert ev_.their_size_usd == pytest.approx(row["source_size_usd"])


def test_a_buy_the_lane_cannot_size_is_skipped_and_the_watchers_valued_row_is_the_one_row(db):
    """Gamma's triage 7 (2026-09-27): notdecu BAfpFwAh, a non-SOL-quote curve. The handoff wrote
    its NULL-size row first, the watcher's valued row was then INSERT OR IGNOREd, and the on-chain
    lane judged the NULL row, which unknown size can only fail at the ratio floor (5 of 12 such
    buys). An unsized handoff now writes nothing; the lane's own log line keeps the refusal."""
    clock, _pools, handoff, ln = _setup(db)
    buy = _buy(FX["untaxxable"], clock)
    assert buy.mint == MINT_2F and buy.sol_amount == 0
    # a non-SOL-quoted curve: slot_lane_price_unreadable until 2026-10-07, now named (pump_curve off)
    assert ln.handle(buy)["reason"] == "slot_lane_curve_quote_not_sol"
    [done] = handoff.flush()
    assert done == {"signature": buy.signature, "handle": "untaxxable", "mint": MINT_2F,
                    "outcome": "skipped_unsized"}
    assert _detections(db.conn) == []
    db.conn.execute(                                  # the watcher, ~1-2 s later, with a size
        "INSERT INTO onchain_wallet_detections (signature, source_user_id, handle, address, mint, "
        "source_size_usd, source_price, detected_at, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (buy.signature, buy.source_user_id, "untaxxable", buy.owner, MINT_2F, 299.0, 1.3e-05,
         RECV, RECV))
    db.conn.commit()
    clock.now += 60
    assert handoff.flush() == []
    [row] = _detections(db.conn)
    assert row["source_size_usd"] == 299.0 and row["source_valuation_json"] is None   # the watcher's


def test_a_pool_that_cannot_be_learned_is_dropped_after_its_one_read(db):
    not_amm = dict(FX["tekkerrss"]["pool_account"], owner="11111111111111111111111111111111")
    clock, pools, handoff, ln = _setup(db, rpc=_PoolRpc(not_amm))
    ln.handle(_buy(FX["tekkerrss"], clock))
    pools.learn_pending()
    [done] = handoff.flush()
    assert done["outcome"] == "pool_unlearnable"
    assert handoff.flush() == [] and _detections(db.conn) == []


def test_a_buy_older_than_the_onchain_lane_accepts_expires_without_a_row(db):
    clock, pools, handoff, ln = _setup(db)
    ln.handle(_buy(FX["tekkerrss"], clock))
    clock.now += 45.1                                # learn never ran: no clip, say
    [done] = handoff.flush()
    assert done["outcome"] == "expired"
    pools.learn_pending()
    assert handoff.flush() == [] and _detections(db.conn) == []


def test_a_row_the_watcher_already_wrote_is_left_alone(db):
    clock, pools, handoff, ln = _setup(db)
    buy = _buy(FX["tekkerrss"], clock)
    db.conn.execute(
        "INSERT INTO onchain_wallet_detections (signature, source_user_id, handle, address, mint, "
        "detected_at, created_at) VALUES (?,?,?,?,?,?,?)",
        (buy.signature, buy.source_user_id, "Tekkerrss", buy.owner, MINT_54, RECV, RECV))
    db.conn.commit()
    ln.handle(buy)
    pools.learn_pending()
    [done] = handoff.flush()
    assert done["outcome"] == "duplicate"
    [row] = _detections(db.conn)
    assert row["source_valuation_json"] is None                  # still the watcher's row


def test_a_shadow_lane_hands_nothing_off(db):
    clock, pools, handoff, ln = _setup(db, mode="shadow")
    ln.handle(_buy(FX["tekkerrss"], clock))
    pools.learn_pending()
    assert handoff.flush() == [] and _detections(db.conn) == []


def test_a_held_write_lock_keeps_the_item_for_the_next_tick_without_stalling(db):
    clock, pools, handoff, ln = _setup(db)
    ln.handle(_buy(FX["tekkerrss"], clock))
    pools.learn_pending()
    blocker = sqlite3.connect(db.path)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        import time
        started = time.monotonic()
        assert handoff.flush() == []                 # locked: kept, nothing lost
        assert time.monotonic() - started < 0.5      # bounded by the short busy timeout
    finally:
        blocker.rollback()
        blocker.close()
    [done] = handoff.flush()
    assert done["outcome"] == "inserted" and len(_detections(db.conn)) == 1


def test_a_stored_handoff_wakes_the_onchain_lane_and_a_duplicate_does_not(db):
    """2026-09-27: the handoff row is swept at once instead of at the lane's next 1 s poll (the
    handoff measured 0.06-1.12 s row -> attempt). The wake follows the stored row."""
    import threading
    for pre_existing, outcome, woken in ((False, "inserted", True), (True, "duplicate", False)):
        clock = _Clock()
        pools = ps.PumpSwapPoolCache(rpc=_PoolRpc())
        wake = threading.Event()
        handoff = lane.Handoff(pools=pools, conn_factory=_factory(db.path), max_age_s=45.0,
                               clock=clock, wake=wake)
        ln = _lane(db, clock, pools, handoff)
        buy = _buy(FX["tekkerrss"], clock)
        db.conn.execute("DELETE FROM onchain_wallet_detections")
        if pre_existing:
            db.conn.execute(
                "INSERT INTO onchain_wallet_detections (signature, source_user_id, handle, address, "
                "mint, detected_at, created_at) VALUES (?,?,?,?,?,?,?)",
                (buy.signature, buy.source_user_id, "Tekkerrss", buy.owner, MINT_54, RECV, RECV))
        db.conn.commit()
        ln.handle(buy)
        pools.learn_pending()
        [done] = handoff.flush()
        assert done["outcome"] == outcome and wake.is_set() is woken


def test_busy_pumpswap_is_enqueued_without_write_and_keeps_original_receipt_clock(db):
    clock, pools, handoff, ln = _setup(db)
    buy = _buy(FX['tekkerrss'], clock)
    assert not handoff.offer_busy(buy, SOL_USD)  # unknown pool: retain ordinary lane/miss learning
    assert handoff.flush() == []
    pools.get(buy.pool)
    pools.learn_pending()
    assert handoff.offer_busy(buy, SOL_USD)
    assert _detections(db.conn) == []  # socket collector did not write
    clock.now += 2  # confirmation aged past the lane's 500ms target
    [done] = handoff.flush()
    assert done['outcome'] == 'inserted'
    [row] = _detections(db.conn)
    assert row['signature'] == buy.signature and row['detected_at'] == RECV
    assert row['source_slot'] == buy.slot and row['source_size_usd'] > 0
    assert handoff.offer_busy(buy, SOL_USD)
    assert handoff.flush()[0]['outcome'] == 'duplicate'
    assert len(_detections(db.conn)) == 1


def test_busy_fallback_waits_for_the_same_wallet_lock_then_executes_once(db, tmp_path):
    import threading
    from sniper.execution.exec_lock import serialize_executor, wallet_exec_lock
    clock, pools, handoff, ln = _setup(db)
    buy = _buy(FX['tekkerrss'], clock)
    pools.get(buy.pool); pools.learn_pending()
    assert handoff.offer_busy(buy, SOL_USD)
    handoff.flush()
    executed, calls = threading.Event(), []
    def execute(conn, ev):
        calls.append(ev.fomo_event_id)
        executed.set()
        return {'status': 'filled'}
    hook = serialize_executor(execute, 'same-wallet', lock_dir=str(tmp_path))
    def consume():
        conn = _factory(db.path)()
        try:
            onchain.sweep_onchain_detections(conn, hook, cursor=0, now_fn=lambda: RECV,
                max_age_s=45, approved_ids_fn=lambda c, n: {buy.source_user_id})
        finally:
            conn.close()
    with wallet_exec_lock('same-wallet', lock_dir=str(tmp_path)):
        thread = threading.Thread(target=consume)
        thread.start()
        assert not executed.wait(.05)
    thread.join(2)
    assert not thread.is_alive() and calls == ['onchain:' + buy.signature]


def test_busy_handoff_keeps_custom_cache_without_pure_getter_on_original_lane(db):
    handoff = lane.Handoff(pools={}, conn_factory=_factory(db.path), clock=_Clock(), max_age_s=45)
    assert not handoff.offer_busy(SimpleNamespace(venue='pump_amm', pool='P'), SOL_USD)


# THE RECV_SLOT LABEL (Gamma LOW-1 on 0e28f2e85): since the crowded-fee change a routed buy's head is
# the one its pair completed at, so the handoff row must not call it a processed receipt.

def test_recv_slot_is_labelled_by_the_head_it_came_from():
    S = 450_000_000
    receipt = SimpleNamespace(slot=S, head_at_recv=S + 1, head_at_recv_mono=1.0, recv_monotonic=1.0, routed=None)
    routed = SimpleNamespace(slot=S, head_at_recv=None, head_at_recv_mono=None, recv_monotonic=1.0,
                             routed=SimpleNamespace(paired_head_slot=S - 1))
    unknown = SimpleNamespace(slot=S, head_at_recv=None, head_at_recv_mono=None, recv_monotonic=1.0,
                              routed=SimpleNamespace(paired_head_slot=None))
    assert lane.recv_slot_fields(receipt) == {"recv_slot": S + 1, "recv_slot_basis": "processed_receipt"}
    assert lane.recv_slot_fields(routed) == {"recv_slot": S - 1, "recv_slot_basis": "routed_pair_complete"}
    assert lane.recv_slot_fields(unknown) == {"recv_slot": None, "recv_slot_basis": None}


@pytest.mark.parametrize("head, basis", [(1, "processed_receipt"), (None, None)])
def test_the_handoff_row_carries_the_recv_slot_and_its_label(db, head, basis):
    import dataclasses
    clock, pools, handoff, ln = _setup(db)
    buy = _buy(FX["tekkerrss"], clock)
    buy = dataclasses.replace(buy, head_at_recv=None if head is None else buy.slot + head,
                              head_at_recv_mono=None if head is None else clock.now)
    assert ln.handle(buy)["reason"] == "pool_unknown"
    pools.learn_pending()
    clock.now += 0.2
    [done] = handoff.flush()
    assert done["outcome"] == "inserted"
    valuation = json.loads(_detections(db.conn)[0]["source_valuation_json"])
    assert valuation["recv_slot"] == (None if head is None else buy.slot + head)
    assert valuation["recv_slot_basis"] == basis
