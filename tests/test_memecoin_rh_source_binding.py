"""A cached source route cannot turn one split leg into the wallet's entire buy."""
import pytest

from sniper.services import memecoin_rh_fast_decide as fast
from test_memecoin_rh_fast_decide import (
    B, CTX, META, SIG, TOKEN, ZPOOL, ZUP_IN, ZUP_PRE, _paid_for, _row,
)

CTX = dict(CTX, allow_estimated_source=False)
SIG = dict(SIG, tx_hash="0x" + "cd" * 32, wallet="0x" + "ef" * 20, source_user_id="uid")


def _bind(tape, *, got=200, pool=ZPOOL, sqrt=ZUP_PRE["sqrt"], venue="v4"):
    from rh_source_fill_fixture import proof_for
    proof = proof_for(SIG, got=got, pool=pool, sqrt=sqrt, venue=venue)
    tape.learn_source_fills([proof])
    return proof


def _source_tape(amounts):
    quote = "0x" + "bb" * 20
    pools = ["0x" + ("%02x" % (17 + i)) * 20 for i in range(len(amounts))]
    meta = {pool: dict(protocol="v3", mint=TOKEN, token0=quote, token1=TOKEN,
                       fee=None, tick_spacing=None, hooks=None) for pool in pools}
    tape = fast.TapeState(meta.get)
    tape.ingest([_row(pool, B, SIG["tx_hash"], 2**96, a0=100, a1=-out, log=i)
                 for i, (pool, out) in enumerate(zip(pools, amounts), 1)])
    return tape


def test_split_outputs_do_not_price_total_payment_against_one_leg():
    # Before the receipt proof is learned, a split waits for it (4663 misses S3, 2026-10-14);
    # it is never priced against one leg.
    tape = _source_tape([100, 200])
    out = fast.decide(dict(SIG, paid=_paid_for(ZUP_IN)), tape=tape, **CTX)
    assert out["reason"] == "source_split_pending"
    assert "src_out" not in out and out.get("tx") is None


def test_a_mint_used_as_an_intermediate_cannot_supply_a_wallet_receipt():
    tape = _source_tape([100, -50])
    out = fast.decide(dict(SIG, paid=_paid_for(ZUP_IN)), tape=tape, **CTX)
    assert out["reason"] == "source_swap_ambiguous"
    assert "src_out" not in out


def test_an_unbound_source_does_not_push_an_unrelated_native_pool():
    tape = fast.TapeState(META.get)
    tape.ingest([_row(ZPOOL, B - 1, "0xother", ZUP_PRE["sqrt"])])
    out = fast.decide(dict(SIG, paid=_paid_for(ZUP_IN)), tape=tape, **CTX)
    assert out["reason"] == "source_swap_pending"
    assert "src_out" not in out and out.get("tx") is None


def test_repeated_identical_log_is_not_a_second_output_leg():
    tape = _source_tape([200])
    row = dict(next(iter(tape._rows.values()))[0])
    tape.ingest([row])
    _bind(tape, pool=row["pool"], sqrt=row["sqrt"], venue="v3")
    out = fast.decide(dict(SIG, paid=_paid_for(ZUP_IN)), tape=tape, **CTX)
    assert out["reason"] == "would_send" and out["src_out"] == 200


def test_conflicting_duplicate_log_does_not_choose_an_output():
    tape = _source_tape([200])
    row = dict(next(iter(tape._rows.values()))[0], amount1=-300)
    tape.ingest([row])
    out = fast.decide(dict(SIG, paid=_paid_for(ZUP_IN)), tape=tape, **CTX)
    assert out["reason"] == "source_swap_ambiguous"


def test_exact_native_source_keeps_its_fast_unsigned_path():
    tape = fast.TapeState(META.get)
    tape.ingest([_row(ZPOOL, B, SIG["tx_hash"], ZUP_PRE["sqrt"], a0=100, a1=-200)])
    _bind(tape)
    out = fast.decide(dict(SIG, paid=_paid_for(ZUP_IN)), tape=tape, **CTX)
    assert out["reason"] == "would_send" and out["route_kind"] == "v4_native"
    assert out["src_out"] == 200 and out["state_kind"] == "tape_post"


@pytest.mark.parametrize("quote_kind", ["weth", "bridge"])
def test_verified_non_native_v4_keeps_wallet_net_fill_in_shared_route(quote_kind):
    from rh_source_fill_fixture import proof_for
    from test_memecoin_rh_fast_routes import (
        A_SQRT, APOOL, B_LIQ, B_SQRT, BPOOL, COIN, USDG, WPOOL2, _brow, _btape,
    )

    pool = WPOOL2 if quote_kind == "weth" else BPOOL
    sqrt = ZUP_PRE["sqrt"] if quote_kind == "weth" else B_SQRT
    sig = dict(SIG, mint=COIN, payment_token=USDG if quote_kind == "bridge" else SIG["payment_token"],
               paid=10**18)
    tape = _btape(_brow(APOOL, B - 2, "0xa", A_SQRT),
                  _brow(pool, B, sig["tx_hash"], sqrt, liq=B_LIQ, a0=1000, a1=-2000))
    # The source wallet receives less than the pool emitted; use its exact net acquisition.
    proof = proof_for(sig, got=1800, pool=pool, sqrt=sqrt)
    tape.learn_source_fills([proof])

    out = fast.decide(sig, tape=tape, **CTX)

    assert out["reason"] == "would_send" and out["route_kind"] == "v4_shared", out
    assert out["src_out"] == 1800 and out["source_fill"] == proof
    assert out.get("tx") is None and "cap_price" not in out


def test_source_arrival_reasks_without_holding_the_ordinary_lane():
    tape = fast.TapeState(META.get)
    pending = fast.PendingSourceSwaps()
    sig = dict(SIG, paid=_paid_for(ZUP_IN))
    assert pending.offer(sig, enqueued=5, now=10)
    assert pending.ready(tape, now=10.1) == []
    tape.ingest([_row(ZPOOL, B, SIG["tx_hash"], ZUP_PRE["sqrt"], a0=100, a1=-200)])
    _bind(tape)
    assert pending.ready(tape, now=10.2) == [(sig, 5)]
    assert pending.ready(tape, now=10.3) == []


def test_first_pool_leg_without_complete_wallet_proof_is_never_priced():
    tape = _source_tape([200])
    out = fast.decide(dict(SIG, paid=_paid_for(ZUP_IN)), tape=tape, **CTX)
    assert out["reason"] == "source_fill_pending"
    assert out.get("tx") is None and "src_out" not in out


def test_wallet_net_acquisition_prices_a_taxed_buy():
    tape = _source_tape([200])
    row = next(iter(tape._rows.values()))[0]
    proof = _bind(tape, got=180, pool=row["pool"], sqrt=row["sqrt"], venue="v3")
    out = fast.decide(dict(SIG, paid=_paid_for(ZUP_IN)), tape=tape, **CTX)
    assert out["reason"] == "would_send" and out["src_out"] == 180
    assert out["paid_usd"] == proof["paid_usd"]
    assert out["source_fill"]["acquired_atomic"] == "180"


def test_wrong_wallet_or_partial_route_proof_cannot_complete_pending_fill():
    tape = _source_tape([200])
    row = next(iter(tape._rows.values()))[0]
    proof = _bind(tape, pool=row["pool"], sqrt=row["sqrt"], venue="v3")
    proof["source_post"] = {"reason": "split_route"}
    tape.learn_source_fills([proof])
    assert fast.decide(dict(SIG, paid=_paid_for(ZUP_IN)), tape=tape, **CTX)["reason"] == "source_swap_ambiguous"


def test_source_fill_cache_reader_skips_bad_json_without_pinning_cursor():
    import sqlite3, json
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE onchain_wallet_detections(id INTEGER PRIMARY KEY, network_id TEXT, source_valuation_json TEXT)")
    tape = _source_tape([200])
    row = next(iter(tape._rows.values()))[0]
    from rh_source_fill_fixture import proof_for
    proof = proof_for(SIG, got=180, pool=row["pool"], sqrt=row["sqrt"], venue="v3")
    db.executemany("INSERT INTO onchain_wallet_detections VALUES(?,?,?)", [(1,"4663","bad"), (2,"56","{}"), (3,"4663",json.dumps({"source_fill":proof}))])
    assert fast.source_fills_once(db, tape, 0) == 3
    assert fast.decide(dict(SIG, paid=_paid_for(ZUP_IN)), tape=tape, **CTX)["src_out"] == 180


def test_shared_chase_cannot_multiply_net_price_by_a_gross_pool_ratio():
    from sniper.services.memecoin_rh_fast_lane import LaneEvent
    from rh_source_fill_fixture import proof_for
    p = dict(SIG, route_kind="v3_shared", pool="0x" + "11" * 20,
             post_sqrt=2**96, src_out=180, paid_usd=100, post_ratio=1.2)
    p["source_fill"] = proof_for(p, venue="v3")
    p["source_fill"]["token_decimals"] = 0
    ev = LaneEvent(p, decimals=0, supply=1000)
    assert ev.their_price == pytest.approx(100/180)
    post = ev.evm_chase_reference.payload()
    assert "ratio" not in post and post["reason"] == "wallet_net_post_units_unproven"


def test_native_decide_payload_passes_independent_receipt_price_binding():
    from sniper.services.memecoin_evm_v4_events import v4_pool_id
    from sniper.services import memecoin_rh_fast_lane as lane
    from test_memecoin_rh_fast_decide import HOOKED, ZUP_OUT
    from rh_source_fill_fixture import proof_for
    pool = v4_pool_id(HOOKED)
    tape = fast.TapeState({pool: META[ZPOOL]}.get)
    tape.ingest([_row(pool, B, SIG["tx_hash"], ZUP_PRE["sqrt"], a0=ZUP_IN, a1=-ZUP_OUT)])
    proof = proof_for(SIG, got=ZUP_OUT, pool=pool, sqrt=ZUP_PRE["sqrt"])
    tape.learn_source_fills([proof])
    out = fast.decide(dict(SIG, paid=_paid_for(ZUP_IN)), tape=tape, **CTX)
    assert out["reason"] == "would_send"
    payload = lane.lane_payload(dict(SIG, permit_nonce=1), out, sent_epoch=1000,
                                inputs={"eth_usd": CTX["eth_usd"]})
    assert lane.native_source_price_matches(payload, out["source_fill"])


def test_native_receipt_payment_is_not_repriced_from_its_usd_valuation():
    from sniper.services.memecoin_evm_v4_events import v4_pool_id
    from sniper.services import memecoin_rh_fast_lane as lane
    from test_memecoin_rh_fast_decide import HOOKED, ZUP_OUT
    from rh_source_fill_fixture import proof_for
    pool = v4_pool_id(HOOKED)
    tape = fast.TapeState({pool: META[ZPOOL]}.get)
    tape.ingest([_row(pool, B, SIG["tx_hash"], ZUP_PRE["sqrt"], a0=ZUP_IN, a1=-ZUP_OUT)])
    proof = proof_for(SIG, got=ZUP_OUT, pool=pool, sqrt=ZUP_PRE["sqrt"], paid_usd=100)
    # Receipt WETH and detection-time USD valuation are separate facts. Current ETH/USD
    # cannot replace the proved payment or enlarge a native source-price cap.
    proof["payment_atomic"] = str(ZUP_IN)
    tape.learn_source_fills([proof])
    out = fast.decide(dict(SIG, paid=_paid_for(ZUP_IN)), tape=tape, **CTX)
    assert out["reason"] == "would_send" and out["src_in_wei"] == ZUP_IN
    payload = lane.lane_payload(dict(SIG, permit_nonce=1), out, sent_epoch=1000,
                                inputs={"eth_usd": CTX["eth_usd"]})
    assert lane.native_source_price_matches(payload, out["source_fill"])
    assert not lane.native_source_price_matches(dict(payload, src_in_wei=ZUP_IN*2), out["source_fill"])


def test_native_post_reference_uses_current_usd_units_and_wallet_net_average():
    from fractions import Fraction
    from sniper.services.memecoin_rh_fast_lane import LaneEvent
    from rh_source_fill_fixture import proof_for
    p = dict(SIG, route_kind="v4_native", pool=ZPOOL, post_sqrt=2**96,
             src_in_wei=10**18, src_out=2*10**18, paid_usd=1000, eth_usd=2500)
    p["source_fill"] = proof_for(p)
    ev = LaneEvent(p, decimals=18, supply=10**21)
    post_usd = Fraction(1 << 192, p["post_sqrt"]**2)*2500
    assert ev.their_price == 500
    assert ev.their_price*ev.evm_chase_reference.post()["ratio"] == pytest.approx(float(post_usd))


# --- an expired re-ask says why (2026-10-06: 4 late 4663 fills; ready() dropped them silently) -------

def _expiry_rows(tmp_path, tape, sig, *, ready_at=None):
    import sqlite3
    db = tmp_path / "decide.db"
    pending = fast.PendingSourceSwaps()
    assert pending.offer(sig, enqueued=5, now=10)
    if ready_at is not None:
        ready_at()
    ready = pending.ready(tape, now=10.1)
    expired = pending.ready(tape, now=13.0)
    for rec, enqueued, blocker in pending.pop_expired():
        fast.record_pending_expired(rec, enqueued, blocker, db_path=str(db), now_perf=6.0)
    conn = sqlite3.connect(db)
    try:
        conn.executescript(fast._DECISIONS)
        return ready + expired, [r[0] for r in conn.execute("SELECT reason FROM rh_fast_decisions")]
    finally:
        conn.close()


def test_an_expired_reask_without_its_proof_writes_one_row(tmp_path):
    tape = fast.TapeState(META.get)
    tape.ingest([_row(ZPOOL, B, SIG["tx_hash"], ZUP_PRE["sqrt"], a0=100, a1=-200)])
    _ready, rows = _expiry_rows(tmp_path, tape, dict(SIG, paid=_paid_for(ZUP_IN)))
    assert rows == ["source_pending_expired:proof_absent"]


def test_an_expired_reask_whose_pool_the_tape_lacks_writes_one_row(tmp_path):
    tape = fast.TapeState(META.get)                     # no swap ingested: the pool is unknown
    _bind(tape, pool="0x" + "e9" * 32, venue="v4")      # 18:53 0x786de93b's shape
    assert tape._source_fills, "the proof must validate, or this test checks proof_absent"
    _ready, rows = _expiry_rows(tmp_path, tape, dict(SIG, paid=_paid_for(ZUP_IN)))
    assert rows == ["source_pending_expired:pool_not_in_tape:v4"]


def test_a_reask_that_becomes_ready_writes_no_expiry_row(tmp_path):
    tape = fast.TapeState(META.get)
    sig = dict(SIG, paid=_paid_for(ZUP_IN))

    def arrive():
        tape.ingest([_row(ZPOOL, B, SIG["tx_hash"], ZUP_PRE["sqrt"], a0=100, a1=-200)])
        _bind(tape)
    ready, rows = _expiry_rows(tmp_path, tape, sig, ready_at=arrive)
    assert ready == [(sig, 5)] and rows == []


def test_the_decide_loop_step_writes_the_expiry_and_redecides_the_ready(tmp_path, monkeypatch):
    """Gamma LOW on 11830c4a4: the loop-level step, not only its parts."""
    import inspect
    import sqlite3
    db = str(tmp_path / "decide.db")
    tape = fast.TapeState(META.get)
    tape.ingest([_row(ZPOOL, B, SIG["tx_hash"], ZUP_PRE["sqrt"], a0=100, a1=-200)])
    pending = fast.PendingSourceSwaps()
    stale = dict(SIG, paid=_paid_for(ZUP_IN))
    assert pending.offer(stale, enqueued=5, now=10)
    redecided = []
    monkeypatch.setattr(fast, "handle_signal", lambda rec, enq, **kw: redecided.append(rec))
    assert fast.service_pending_sources(pending, tape=tape, live={}, db_path=db, now_fn=None,
                                        on_decided=None, now=13.0) == 0
    conn = sqlite3.connect(db)
    rows = [r[0] for r in conn.execute("SELECT reason FROM rh_fast_decisions")]
    conn.close()
    assert rows == ["source_pending_expired:proof_absent"] and redecided == []
    fresh = dict(stale, tx_hash="0x" + "ce" * 32)
    pending.offer(fresh, enqueued=5, now=20)
    tape.ingest([_row(ZPOOL, B + 1, fresh["tx_hash"], ZUP_PRE["sqrt"], a0=100, a1=-200)])
    _bind_sig = dict(SIG, tx_hash=fresh["tx_hash"])
    from rh_source_fill_fixture import proof_for
    tape.learn_source_fills([proof_for(_bind_sig, got=200, pool=ZPOOL, sqrt=ZUP_PRE["sqrt"])])
    assert fast.service_pending_sources(pending, tape=tape, live={}, db_path=db, now_fn=None,
                                        on_decided=None, now=20.1) == 1 and redecided == [fresh]
    assert "service_pending_sources(" in inspect.getsource(fast.start_decide)


def test_a_source_that_resolved_by_the_deadline_is_named_so():
    # Gamma LOW-2 on 11830c4a4: ready() tests the deadline first, so this blocker means the source
    # resolved between the last ready() pass and the deadline check, not that it is still pending.
    tape = fast.TapeState(META.get)
    tape.ingest([_row(ZPOOL, B, SIG["tx_hash"], ZUP_PRE["sqrt"], a0=100, a1=-200)])
    _bind(tape)
    assert tape.source_blocker(dict(SIG, paid=_paid_for(ZUP_IN))) == "resolved_at_deadline"


def test_a_deposit_proved_shared_source_reasks_until_its_receipt_then_sends(tmp_path, monkeypatch):
    """4663 proof refusals 10-07..10-14: 11 armed deposit-proved v3/v4_shared signals were sent
    before their receipt, and the lane's shared executor refused every one source_fill_unproved.
    The deposit path now re-asks a shared route until its receipt arrives, then sends it once."""
    from rh_source_fill_fixture import proof_for
    from sniper.services.memecoin_rh_fast_lane import make_lane_sender
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_RH_FAST_LANE_REQUIRE_EXACT_STATE", False)
    tape = _source_tape([200])                      # the source's own swap in one V3 pool, no receipt
    pool = next(iter(tape._meta))
    live = {k: v for k, v in CTX.items() if k not in ("allow_estimated_source", "now")}
    rec = dict(SIG, paid=_paid_for(ZUP_IN), deposit_proof={"order_id": "0x" + "aa" * 32, "origin_tx": "x"},
               deposit_status="matched")
    sent = []                                       # production's live hand-off: would_send only
    on_decided = make_lane_sender("live", "lane", send_fn=lambda _addr, payload: sent.append(payload))
    kw = dict(tape=tape, live=live, db_path=str(tmp_path / "d.db"), now_fn=lambda: CTX["now"],
              on_decided=on_decided, deposits=object())
    out = fast.handle_signal(rec, 0.0, **kw)
    assert out["reason"] == "source_fill_pending" and sent == [], out
    # Without a deposit proof the receipt wait is today's, and nothing marks the signal.
    plain = dict(SIG, paid=_paid_for(ZUP_IN))
    assert fast.handle_signal(plain, 0.0, **dict(kw, deposits=None))["reason"] == "source_fill_pending"
    assert "needs_source_fill" not in plain and sent == []
    pending = fast.PendingSourceSwaps(wait_s=2.0)
    assert pending.offer(rec, enqueued=0.0, now=0.0)
    assert pending.ready(tape, now=0.5) == []       # the tape swap alone does not release it
    proof = proof_for(SIG, got=200, pool=pool, sqrt=2**96, venue="v3")
    tape.learn_source_fills([proof])
    assert fast.service_pending_sources(pending, now=0.6, **kw) == 1
    assert [p["decide_reason"] for p in sent] == ["would_send"], sent
    assert sent[0]["route_kind"] == "v3_shared" and sent[0]["source_fill"] == proof


def _onchain_verdict_then_shared(verdict):
    """The on-chain lane judges a 4663 source buy first (no datagram before the receipt), then the
    fast lane's shared route judges the same buy after it (Gamma LOW on 59bdefa72)."""
    from collections import deque
    from types import SimpleNamespace
    from rh_source_fill_fixture import proof_for
    from sniper.services import memecoin_bsc_fast_lane as bsc
    from sniper.services import memecoin_onchain_lane as ol
    from sniper.services import memecoin_rh_fast_lane as lane
    from test_memecoin_pons_counter_route import CURVE, MINT, TAKER
    bsc.clear_policy_decisions()
    sig = dict(SIG, mint=MINT, pool=CURVE, src_out=10**24, paid_usd=100,
               route_kind="pons_shared", post_ratio=None)
    sig["source_fill"] = proof_for(sig, venue="pons_v2")
    sig["source_fill"]["source_post"] = dict(
        reason="pons_post_state_unavailable", venue="pons_v2", pool=CURVE, pool_input_atomic="100",
        pool_output_atomic=str(10**24), receipt_routes_complete=True)
    row = dict(id=1, signature="4663:%s:%s" % (sig["tx_hash"], MINT), source_user_id="uid",
               handle="h", address=sig["wallet"], source_slot=sig["seq"], network_id="4663",
               mint=MINT, source_size_usd=100.0, source_price=None,
               detected_at="2026-10-14T16:00:00+00:00", block_time=None,
               source_valuation_json="{}", source_observation_state=None)
    assert ol._run_hook(None, lambda conn, ev: dict(verdict), row) == verdict
    live = lane.LiveLane.__new__(lane.LiveLane)
    live.stopped = False
    live.cache = SimpleNamespace(stats=lambda mint, now: None, put_market_stats=lambda *a, **k: None)
    live.stats_fn = lambda mint: None
    live.host_clock = lambda: 1005.0
    live.ctx, live.contexts, live.conn = dict(address=TAKER), {}, None
    live.clip_usd, live.fills, live.send_times = 50, 0, deque()
    hooked = []

    def factory(contexts, stats_fn):
        def hook(conn, ev):
            hooked.append(ev)
            return dict(status="rejected", reason="fixture_only")
        return hook
    live.shared_hook_factory = factory
    try:
        return live._handle_shared(sig, {}), hooked
    finally:
        bsc.clear_policy_decisions()


def test_an_onchain_chase_refusal_is_final_for_the_fast_lanes_later_shared_send():
    res, hooked = _onchain_verdict_then_shared(
        {"status": "skipped_chase", "reason": "chase 41.0% over cap 30.0%"})
    assert res == {"status": "rejected",
                   "reason": "onchain_decided: hook:skipped_chase:chase 41.0% over cap 30.0%"}
    assert hooked == []                                  # never re-judged on a fresher quote


def test_an_onchain_no_quote_leaves_the_fast_lanes_shared_send_open():
    res, hooked = _onchain_verdict_then_shared(
        {"status": "aborted", "reason": "no quote (provider_discovery_rpc_failed:timeout)"})
    assert res["reason"] == "fixture_only" and len(hooked) == 1
