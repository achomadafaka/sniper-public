"""4663 fast lane routes (spec 2026-10-02-memecoin-4663-fast-lane-routes-design.md rev 2).

R1 learn: the in-memory tape knew only pools that swapped since the process started, so 4 of 10
routing refusals had a native V4 pool in pool_meta it never loaded. A learn thread loads a mint's
pools and each pool's newest swap from the tape DB, off the tape and decide threads; a learned
pre-state routes only within the tape lag limit (Gamma M2)."""
import sqlite3

import pytest

from sniper.services import memecoin_rh_fast_decide as fast
from test_memecoin_rh_fast_decide import (B, CTX, HOOK, META, NATIVE, SIG, TOKEN, ZPOOL, ZUP_IN, ZUP_PRE,
                                          _paid_for, _row, _tape_db, _tick)


def _learned(block):
    return [(ZPOOL, META[ZPOOL], _row(ZPOOL, block, "0xold", ZUP_PRE["sqrt"]))]


def test_a_learned_pool_routes_and_its_row_is_marked_learned():
    tape = fast.TapeState({}.get)
    assert tape.route(TOKEN, now=1000.0) == (None, "no_pool")
    tape.learn(_learned(B - 3))
    pool, _key = tape.route(TOKEN, now=1000.0)
    kind, row = tape.state(pool, B, "0xsrc")
    assert pool == ZPOOL and kind == "pre" and row["learned"] is True


def test_a_learned_state_within_the_lag_limit_routes_and_an_older_one_is_learned_stale():
    fresh = fast.TapeState({}.get)
    fresh.learn(_learned(B - 3))
    fresh.head = B - 1
    assert fast.decide(dict(SIG, paid=_paid_for(ZUP_IN)), tape=fresh, **CTX)["reason"] == "would_send"
    stale = fast.TapeState({}.get)
    stale.learn(_learned(B - 50))                                  # a quiet pool's row, hours old
    stale.head = B - 1
    assert fast.decide(dict(SIG, paid=_paid_for(ZUP_IN)), tape=stale, **CTX)["reason"] == "learned_stale"


def test_an_old_live_swap_after_learning_still_needs_the_bound_source_swap():
    tape = fast.TapeState(META.get)
    tape.learn(_learned(B - 60))
    tape.ingest([_row(ZPOOL, B - 50, "0xlive", ZUP_PRE["sqrt"])])
    tape.head = B - 1
    assert fast.decide(dict(SIG, paid=_paid_for(ZUP_IN)), tape=tape, **CTX)["reason"] == "state_stale"


def test_learning_never_replaces_a_newer_live_row():
    tape = fast.TapeState(META.get)
    tape.ingest([_row(ZPOOL, B - 2, "0xlive", ZUP_PRE["sqrt"])])
    tape.learn(_learned(B - 9))
    kind, row = tape.state(ZPOOL, B, "0xsrc")
    assert row["tx_sig"] == "0xlive" and not row.get("learned")
    # state() picks the newest block anyway; the guard keeps an older learned row out of the 16-deep
    # history, where it would only push live rows out (Gamma LOW on 4c2fd530b).
    assert [r["tx_sig"] for r in tape._rows[ZPOOL]] == ["0xlive"]


def test_learn_reads_the_mints_pools_and_each_pools_newest_swap(tmp_path):
    _tape_db(tmp_path / "t.db", [_tick(B - 9, "0xa", 11), _tick(B - 4, "0xb", 12), _tick(B - 2, "0xc", 13, chain="56")])
    conn = fast.open_tape(str(tmp_path / "t.db"))
    got = fast.learn_pools(conn, fast.meta_reader(conn), TOKEN)
    assert [(p, row["tx_sig"], row["sqrt"]) for p, _m, row in got] == [(ZPOOL, "0xb", 12)]
    assert fast.learn_pools(conn, fast.meta_reader(conn), "0xnone") == []


def test_a_no_route_decision_offers_the_mint_to_the_learn_queue(tmp_path):
    offered = []
    fast.handle_signal(dict(SIG, paid=_paid_for(ZUP_IN)), 0.0, tape=fast.TapeState({}.get), live=dict(CTX),
                       db_path=str(tmp_path / "d.db"), now_fn=lambda: 1000.0, learn=offered.append)
    assert offered == [TOKEN]


def test_the_learn_key_reader_is_read_only(tmp_path):
    """Gamma: the learn thread's ledger key reader is a long-lived READ-ONLY handle, never a writable
    sniper_tracker.db handle for an observer."""
    from sniper.persistence import db_memecoin as m
    ledger = tmp_path / "ledger.db"
    c = sqlite3.connect(ledger)
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    c.close()
    read = fast.ledger_key_reader(str(ledger))
    with pytest.raises(sqlite3.OperationalError):
        read.conn.execute("CREATE TABLE x (a)")


# --- R2 two-hop V4 through a bridge token, R3 WETH-quoted V4 (decide side, sign-less M1) -----------
from fractions import Fraction  # noqa: E402

from sniper.execution import evm_v4_quote as v4  # noqa: E402
from test_memecoin_rh_fast_decide import USDG, WETH  # noqa: E402

COIN = "0xcace05716778f86e4feb04bd7a2c2f2c31705f2b"          # sorts after USDG: USDG is currency0
APOOL, BPOOL, WPOOL2 = "0x" + "a1" * 32, "0x" + "b2" * 32, "0x" + "c3" * 32
HOOK2 = "0xe5e702641ea86f4ae6cc3cdaed2b886f976be044"
BMETA = {
    APOOL: dict(protocol="v4", mint=USDG, token0=NATIVE, token1=USDG, fee=500, tick_spacing=10, hooks=NATIVE),
    BPOOL: dict(protocol="v4", mint=COIN, token0=USDG, token1=COIN, fee=10000, tick_spacing=200, hooks=HOOK2),
    WPOOL2: dict(protocol="v4", mint=COIN, token0=WETH, token1=COIN, fee=3000, tick_spacing=60, hooks=NATIVE),
}
A_SQRT = 4142161624346 * 2 ** 96 // 10 ** 12          # ~ USDG atomic per wei 1.7e-9 x 1e12 ...
B_SQRT = 3 * 2 ** 96                                  # 9 coin atomic per USDG atomic
B_LIQ = 10 ** 24


def _brow(pool, block, tx, sqrt, liq=10 ** 22, a0=1, a1=-1):
    return dict(pool=pool, block_no=block, log_index=0, tx_sig=tx, amount0=a0, amount1=a1, liquidity=liq,
                sqrt=sqrt, observed=1000.0)


def _btape(*rows):
    tape = fast.TapeState(BMETA.get)
    tape.ingest(list(rows))
    tape.head = B - 1
    return tape


def test_one_for_zero_math_matches_the_exact_formula():
    sqrt, liq, d = 3 * 2 ** 96, 10 ** 24, 10 ** 18
    after, out = fast.swap_one_for_zero(sqrt, liq, d)
    assert after == sqrt + (d << 96) // liq
    assert out == ((liq << 96) * (after - sqrt) // after) // sqrt
    assert out < d * Fraction(2 ** 192, sqrt ** 2)                      # less than at the mid price
    assert fast.swap_one_for_zero(0, liq, d) is None and fast.swap_one_for_zero(sqrt, liq, 0) is None


def test_the_bridge_route_pairs_a_native_x_pool_with_the_coins_x_pool():
    tape = _btape(_brow(APOOL, B - 2, "0xa", A_SQRT), _brow(BPOOL, B - 2, "0xb", B_SQRT, liq=B_LIQ))
    assert tape.route(COIN, now=1000.0) == (None, "no_native_v4_pool")
    r = tape.bridge_route(COIN, now=1000.0)
    assert (r["bridge"], r["a_pool"], r["b_pool"], r["x_is_c0"]) == (USDG, APOOL, BPOOL, True)


def test_a_bridged_buy_converts_at_the_mid_and_caps_on_the_coins_pool():
    """Gamma M1: X is converted at A's MID rate, so hop A's fee and impact can only tighten the cap."""
    src_in_x, src_out = 10 ** 8, 899_000_000                        # the source's own swap in B, on the tape
    post_b = B_SQRT + 1000
    tape = _btape(_brow(APOOL, B - 2, "0xa", A_SQRT),
                  _brow(BPOOL, B, "0xsrc", post_b, liq=B_LIQ, a0=src_in_x, a1=-src_out))
    rec = fast.decide(dict(SIG, mint=COIN, payment_token=USDG, paid=-(-src_in_x * 100 // 99)), tape=tape, **CTX)
    assert rec["reason"] == "would_send" and rec["route_kind"] == "v4_bridge" and rec["state_kind"] == "tape_post"
    clip_wei = int(Fraction(170) / Fraction(2500) * 10 ** 18)
    x_mid = clip_wei * A_SQRT ** 2 >> 192
    post_x_per_coin = Fraction(2 ** 192, post_b ** 2)
    cap = min(post_x_per_coin * Fraction(13, 10), Fraction(src_in_x, src_out) * Fraction(3, 2))
    assert rec["x_mid"] == x_mid and rec["min_out"] == x_mid * cap.denominator // cap.numerator
    from sniper.execution.evm_v4_multihop_tx import native_multihop_buy_tx
    want = native_multihop_buy_tx("4663", COIN, [(USDG, 500, 10, NATIVE), (COIN, 10000, 200, HOOK2)],
                                  clip_wei, rec["min_out"], 1060)
    assert rec["tx"] == want and int(want["value"], 16) == clip_wei


def test_a_native_route_wins_over_a_bridge():
    npool = "0x" + "d4" * 32
    meta = dict(BMETA, **{npool: dict(protocol="v4", mint=COIN, token0=NATIVE, token1=COIN, fee=0,
                                      tick_spacing=200, hooks=HOOK2)})
    tape = fast.TapeState(meta.get)
    tape.ingest([_brow(APOOL, B - 2, "0xa", A_SQRT), _brow(BPOOL, B - 2, "0xb", B_SQRT, liq=B_LIQ),
                 _brow(npool, B - 2, "0xn", ZUP_PRE["sqrt"], liq=ZUP_PRE["liquidity"])])
    tape.head = B - 1
    rec = fast.decide(dict(SIG, mint=COIN, paid=_paid_for(ZUP_IN)), tape=tape, **CTX)
    assert rec["route_kind"] == "v4_native" and rec["pool"] == npool


def test_a_bridge_leg_without_state_is_a_named_fallback():
    tape = _btape(_brow(BPOOL, B - 2, "0xb", B_SQRT, liq=B_LIQ))
    assert fast.decide(dict(SIG, mint=COIN, paid=10 ** 18), tape=tape, **CTX)["reason"] == "no_native_v4_pool"


def test_a_weth_pool_buy_wraps_and_swaps_from_the_router():
    tape = _btape(_brow(WPOOL2, B - 2, "0xw", ZUP_PRE["sqrt"], liq=ZUP_PRE["liquidity"]))
    rec = fast.decide(dict(SIG, mint=COIN, paid=_paid_for(ZUP_IN)), tape=tape, **CTX)
    assert rec["reason"] == "would_send" and rec["route_kind"] == "v4_weth" and rec["pool"] == WPOOL2
    data = rec["tx"]["data"]
    assert data.startswith("0x" + v4._SEL_UR_EXECUTE) and "0b10" in data[2 + 8 + 64 * 4: 2 + 8 + 64 * 5]
    assert int(rec["tx"]["value"], 16) == rec["clip_wei"]


def test_the_sender_sends_every_kind_and_the_runner_gates_live():
    """Routes rev 3 (6.3): the runner shadow judges every kind; MEMECOIN_RH_FAST_ROUTES gates live."""
    from sniper.services.memecoin_rh_fast_lane import make_lane_sender
    sent = []
    on = make_lane_sender("live", "/tmp/nowhere", send_fn=lambda a, p: sent.append(p))
    for kind in ("v4_native", "v4_bridge", "v4_weth", None):
        out = {"reason": "would_send", "route_kind": kind, "pool": "p", "key": {}, "cap_price": Fraction(1, 2),
               "clip_wei": 1, "min_out": 2}
        on(dict(SIG, permit_nonce=1), out)
    assert [p["route_kind"] for p in sent] == ["v4_native", "v4_bridge", "v4_weth", None]


def test_m1_records_the_route_kind_and_the_bridge_leg(tmp_path):
    import sqlite3 as _sq
    src_in_x, src_out = 10 ** 8, 899_000_000
    tape = _btape(_brow(APOOL, B - 2, "0xa", A_SQRT),
                  _brow(BPOOL, B, "0xsrc", B_SQRT + 1000, liq=B_LIQ, a0=src_in_x, a1=-src_out))
    db = str(tmp_path / "d.db")
    fast.handle_signal(dict(SIG, mint=COIN, payment_token=USDG, paid=-(-src_in_x * 100 // 99)), 0.0, tape=tape,
                       live=dict(CTX), db_path=db, now_fn=lambda: 1000.0)
    row = _sq.connect(db).execute("SELECT reason, route_kind, bridge, bridge_pool, a_state_kind, x_mid "
                                  "FROM rh_fast_decisions").fetchone()
    assert row[:5] == ("would_send", "v4_bridge", USDG, APOOL, "pre") and int(row[5]) > 0
