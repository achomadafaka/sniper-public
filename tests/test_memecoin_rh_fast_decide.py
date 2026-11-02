"""4663 fast lane M1, the decide path in shadow (spec 2026-10-01-memecoin-4663-fast-lane-design.md
§2.3-§2.5, §6 M1). Sign-less: it builds the unsigned buy the lane would send and records why it
would not. The calldata is decoded here with eth_abi, independently of the encoder under test."""
import pytest
from eth_abi import decode

from sniper.services import memecoin_rh_fast_decide as fast

UR = "0x8876789976decbfcbbbe364623c63652db8c0904"
NATIVE = "0x" + "00" * 20
TOKEN = "0xcabb5484150745a3349b956da5d6e97bd7c8dfd9"
HOOK = "0xe5e702641ea86f4ae6cc3cdaed2b886f976be044"
TAKER = "0x1111111111111111111111111111111111111111"
HOOKED = dict(currency0=NATIVE, currency1=TOKEN, fee=0, tick_spacing=200, hooks=HOOK)
PLAIN = dict(currency0=NATIVE, currency1=TOKEN, fee=39000, tick_spacing=390, hooks=NATIVE)
AMOUNT, MIN_OUT, DEADLINE = 4 * 10 ** 16, 123_456_789 * 10 ** 9, 1_790_000_000


def _execute(tx):
    data = bytes.fromhex(tx["data"][2:])
    assert data[:4].hex() == "3593564c"                                 # execute(bytes,bytes[],uint256)
    commands, inputs, deadline = decode(["bytes", "bytes[]", "uint256"], data[4:])
    actions, params = decode(["bytes", "bytes[]"], inputs[0])
    return commands, deadline, actions, params


def test_hooked_native_buy_is_the_path_form_paid_by_value():
    tx = fast.native_buy_tx(HOOKED, AMOUNT, MIN_OUT, DEADLINE, taker=TAKER)
    commands, deadline, actions, params = _execute(tx)
    assert tx["to"] == UR and int(tx["value"], 16) == AMOUNT and tx["gas"] == "1200000"
    assert commands == bytes([0x10]) and deadline == DEADLINE
    assert actions == bytes([0x07, 0x0B, 0x0E])                         # SWAP_EXACT_IN, SETTLE, TAKE
    # 4663's deployed ExactInputParams has FIVE fields (an empty bytes before the amounts).
    (cin, path, extra, amount_in, min_out), = decode(
        ["(address,(address,uint24,int24,address,bytes)[],bytes,uint128,uint128)"], params[0])
    assert (cin, extra, amount_in, min_out) == (NATIVE, b"", AMOUNT, MIN_OUT)
    assert path == ((TOKEN, 0, 200, HOOK, b""),)
    assert decode(["address", "uint256", "bool"], params[1]) == (NATIVE, 0, False)   # router pays
    assert decode(["address", "address", "uint256"], params[2]) == (TOKEN, TAKER, 0)


def test_plain_native_buy_is_the_single_form():
    tx = fast.native_buy_tx(PLAIN, AMOUNT, MIN_OUT, DEADLINE, taker=TAKER)
    commands, deadline, actions, params = _execute(tx)
    assert int(tx["value"], 16) == AMOUNT and tx["gas"] == "700000"
    assert actions == bytes([0x06, 0x0C, 0x0F])                         # SINGLE, SETTLE_ALL, TAKE_ALL
    (key, zero_for_one, amount_in, min_out, hook_data), = decode(
        ["((address,address,uint24,int24,address),bool,uint128,uint128,bytes)"], params[0])
    assert key == (NATIVE, TOKEN, 39000, 390, NATIVE)
    assert (zero_for_one, amount_in, min_out, hook_data) == (True, AMOUNT, MIN_OUT, b"")
    assert decode(["address", "uint256"], params[1]) == (NATIVE, AMOUNT)
    assert decode(["address", "uint256"], params[2]) == (TOKEN, MIN_OUT)


@pytest.mark.parametrize("key, amount, min_out", [
    (dict(HOOKED, currency0="0x0bd7d308f8e1639fab988df18a8011f41eacad73"), AMOUNT, MIN_OUT),
    (HOOKED, AMOUNT, 0),                    # no minimum is no price cap: never built
    (HOOKED, 0, MIN_OUT),
    (HOOKED, 1 << 128, MIN_OUT),
    (HOOKED, AMOUNT, 1 << 128),
])
def test_refuses_anything_but_a_capped_native_buy(key, amount, min_out):
    with pytest.raises(ValueError):
        fast.native_buy_tx(key, amount, min_out, DEADLINE, taker=TAKER)


# Two real 4663 tape swaps (pool_ticks, pool perspective: amount0 > 0 is native INTO the pool).
# DOTTIE plain pool, fee 10000 plus the 1000-pip protocol fee: total 10990 pips.
DOTTIE_PRE = dict(sqrt=576656441049335054629002240944851, liquidity=43207453905152099780443)
DOTTIE_IN, DOTTIE_OUT = 8000913107616054, 418636267317264719902785
DOTTIE_POST = 575888800764531735126135280727089
# ZUPITER hooked pool, fee 0: the hook's fee is taken before the pool, so amount0 is what it saw.
ZUP_PRE = dict(sqrt=883357915159877246598007788020927, liquidity=29277002188455995748088)
ZUP_IN, ZUP_OUT = 25854436920000000, 3182688391454788035426447
ZUP_POST = 874745060896749537239184449426278


@pytest.mark.parametrize("pre, amount_in, out, post", [
    (DOTTIE_PRE, DOTTIE_IN * (10 ** 6 - 10990) // 10 ** 6, DOTTIE_OUT, DOTTIE_POST),
    (ZUP_PRE, ZUP_IN, ZUP_OUT, ZUP_POST),
])
def test_single_range_math_reproduces_real_tape_swaps_exactly(pre, amount_in, out, post):
    assert fast.swap_native_in(pre["sqrt"], pre["liquidity"], amount_in) == (post, out)


def test_swap_math_refuses_an_empty_pool():
    assert fast.swap_native_in(ZUP_PRE["sqrt"], 0, ZUP_IN) is None
    assert fast.swap_native_in(0, ZUP_PRE["liquidity"], ZUP_IN) is None
    assert fast.swap_native_in(ZUP_PRE["sqrt"], ZUP_PRE["liquidity"], 0) is None


def _wei_per_atomic(sqrt):
    from fractions import Fraction
    return Fraction(1 << 192, sqrt * sqrt)


def test_the_cap_is_the_tighter_of_post_trade_chase_and_source_premium():
    from fractions import Fraction
    cap = fast.price_cap(post_sqrt=ZUP_POST, src_in=ZUP_IN, src_out=ZUP_OUT,
                         chase_pct=30, premium=1.5)
    post = _wei_per_atomic(ZUP_POST)
    avg = Fraction(ZUP_IN, ZUP_OUT)
    assert cap == min(post * Fraction(13, 10), avg * Fraction(3, 2))
    # One term off is the other term alone; both off is no cap at all (never a zero).
    assert fast.price_cap(post_sqrt=ZUP_POST, src_in=ZUP_IN, src_out=ZUP_OUT,
                          chase_pct=0, premium=1.5) == avg * Fraction(3, 2)
    assert fast.price_cap(post_sqrt=ZUP_POST, src_in=ZUP_IN, src_out=ZUP_OUT,
                          chase_pct=30, premium=0) == post * Fraction(13, 10)
    assert fast.price_cap(post_sqrt=ZUP_POST, src_in=ZUP_IN, src_out=ZUP_OUT,
                          chase_pct=0, premium=0) is None


def test_min_out_is_the_clip_at_the_cap_price_rounded_down():
    from fractions import Fraction
    cap = Fraction(7, 3)                            # wei per token atomic
    assert fast.min_out_at(10 ** 16, cap) == 10 ** 16 * 3 // 7
    assert fast.min_out_at(10 ** 16, None) is None


ZPOOL, WPOOL, PPOOL = "0x" + "a1" * 32, "0x" + "b2" * 32, "0x" + "c3" * 32
WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
META = {
    ZPOOL: dict(protocol="v4", mint=TOKEN, token0=NATIVE, token1=TOKEN, fee=0, tick_spacing=200,
                hooks=HOOK),
    PPOOL: dict(protocol="v4", mint=TOKEN, token0=NATIVE, token1=TOKEN, fee=10000, tick_spacing=200,
                hooks=NATIVE),
    WPOOL: dict(protocol="v4", mint="0xweth-only", token0=WETH, token1="0xweth-only", fee=0,
                tick_spacing=200, hooks=HOOK),
}
B = 77689412                                        # the source's block


def _row(pool, block, tx, sqrt, liquidity=ZUP_PRE["liquidity"], a0=1, a1=-1, t=1000.0, log=0):
    return dict(pool=pool, block_no=block, log_index=log, tx_sig=tx, amount0=a0, amount1=a1,
                liquidity=liquidity, sqrt=sqrt, observed=t)


def _tape(rows):
    tape = fast.TapeState(META.get)
    tape.ingest(rows)
    return tape


def test_the_route_is_the_busiest_native_v4_pool_of_the_mint():
    tape = _tape([_row(ZPOOL, B - 5, "0x1", ZUP_PRE["sqrt"]), _row(ZPOOL, B - 4, "0x2", 1),
                  _row(PPOOL, B - 4, "0x3", 1), _row(WPOOL, B - 4, "0x4", 1)])
    pool, key = tape.route(TOKEN, now=1000.0)
    assert pool == ZPOOL and key == dict(currency0=NATIVE, currency1=TOKEN, fee=0,
                                         tick_spacing=200, hooks=HOOK)
    assert tape.route("0xweth-only", now=1000.0) == (None, "no_native_v4_pool")
    assert tape.route("0xunknown", now=1000.0) == (None, "no_pool")


def test_a_pool_without_meta_is_ignored():
    tape = fast.TapeState({}.get)
    tape.ingest([_row(ZPOOL, B - 1, "0x1", 1)])
    assert tape.route(TOKEN, now=1000.0) == (None, "no_pool")


def test_the_state_is_the_source_swap_itself_or_the_last_swap_before_its_block():
    pre = _row(ZPOOL, B - 3, "0xpre", ZUP_PRE["sqrt"])
    same_block = _row(ZPOOL, B, "0xother", 7, log=9)
    source = _row(ZPOOL, B, "0xsrc", ZUP_POST, a0=ZUP_IN, a1=-ZUP_OUT, log=3)
    assert _tape([pre, same_block]).state(ZPOOL, B, "0xsrc") == ("pre", pre)
    assert _tape([pre, same_block, source]).state(ZPOOL, B, "0xsrc") == ("tape_post", source)
    assert _tape([same_block]).state(ZPOOL, B, "0xsrc") == (None, "no_state")


SIG = dict(tx_hash="0xsrc", seq=B, mint=TOKEN, payment_token=WETH, wallet="0xw", signal=True)
CTX = dict(eth_usd=2500.0, clip_usd=170.0, chase_pct=30, premium=1.5, fee_pct=1.0,
           max_lag_blocks=8, float_wei=10 ** 18, gas_price_wei=4 * 10 ** 7, low_wei=2 * 10 ** 15,
           decimals={WETH: 18, USDG: 6}, taker=TAKER, now=1000.0,
           allow_estimated_source=True)  # Explicit sign-less historical estimator fixtures only.


def _paid_for(pool_input):
    """The WETH payment whose 1% haircut leaves exactly `pool_input` at the pool."""
    return -(-pool_input * 100 // 99)


def test_a_signal_with_fresh_state_would_send_a_capped_buy():
    from fractions import Fraction
    tape = _tape([_row(ZPOOL, B - 3, "0xpre", ZUP_PRE["sqrt"])])
    rec = fast.decide(dict(SIG, paid=_paid_for(ZUP_IN)), tape=tape, **CTX)
    assert rec["reason"] == "would_send" and rec["state_kind"] == "pre" and rec["pool"] == ZPOOL
    assert rec["state_age_blocks"] == 3 and rec["est_post_sqrt"] == ZUP_POST
    clip_wei = int(Fraction(170) / Fraction(2500) * 10 ** 18)
    cap = min(Fraction(1 << 192, ZUP_POST ** 2) * Fraction(13, 10),
              Fraction(ZUP_IN, ZUP_OUT) * Fraction(3, 2))
    assert rec["clip_wei"] == clip_wei and rec["min_out"] == clip_wei * cap.denominator // cap.numerator
    assert int(rec["tx"]["value"], 16) == clip_wei and rec["tx"]["gas"] == "1200000"


def test_an_old_quiet_pool_needs_the_bound_source_swap_even_when_the_tape_caught_up():
    # Tape head proves freshness, not which pool the wallet bought through. The pending-source
    # lane can re-ask from that exact receipt; no imaginary buy may price an unrelated old pool.
    tape = _tape([_row(ZPOOL, B - 50, "0xpre", ZUP_PRE["sqrt"]), _row(WPOOL, B - 1, "0xx", 1)])
    rec = fast.decide(dict(SIG, paid=_paid_for(ZUP_IN)), tape=tape, **CTX)
    assert rec["reason"] == "state_stale" and rec["state_age_blocks"] == 50
    assert rec["tape_lag_blocks"] == 1


def test_a_source_swap_already_on_the_tape_gives_the_exact_post_state():
    tape = _tape([_row(ZPOOL, B - 3, "0xpre", 5),
                  _row(ZPOOL, B, "0xsrc", ZUP_POST, a0=ZUP_IN, a1=-ZUP_OUT)])
    rec = fast.decide(dict(SIG, paid=1), tape=tape, **CTX)        # paid is unused on this path
    assert rec["reason"] == "would_send" and rec["state_kind"] == "tape_post"
    assert (rec["est_post_sqrt"], rec["src_in_wei"], rec["src_out"]) == (ZUP_POST, ZUP_IN, ZUP_OUT)


@pytest.mark.parametrize("rows, sig, ctx, reason", [
    ([_row(ZPOOL, B - 9, "0xpre", ZUP_PRE["sqrt"])], {}, {}, "tape_behind"),
    ([], {}, {}, "no_pool"),
    ([_row(ZPOOL, B - 1, "0xpre", ZUP_PRE["sqrt"], liquidity=0)], {}, {}, "no_liquidity"),
    ([_row(ZPOOL, B - 1, "0xpre", ZUP_PRE["sqrt"])], {}, dict(eth_usd=None), "no_eth_usd"),
    ([_row(ZPOOL, B - 1, "0xpre", ZUP_PRE["sqrt"])], dict(payment_token="0xodd"), {},
     "payment_decimals_unknown"),
    ([_row(ZPOOL, B - 1, "0xpre", ZUP_PRE["sqrt"])], {}, dict(chase_pct=0, premium=0),
     "no_price_cap"),
    ([_row(ZPOOL, B - 1, "0xpre", ZUP_PRE["sqrt"])], {}, dict(float_wei=None), "float_unknown"),
    ([_row(ZPOOL, B - 1, "0xpre", ZUP_PRE["sqrt"])], {}, dict(gas_price_wei=None),
     "float_unknown"),
    ([_row(ZPOOL, B - 1, "0xpre", ZUP_PRE["sqrt"])], {}, dict(float_wei=10 ** 16), "float_short"),
])
def test_every_missing_or_unknown_input_is_a_named_fallback(rows, sig, ctx, reason):
    rec = fast.decide(dict(SIG, paid=_paid_for(ZUP_IN), **sig), tape=_tape(rows), **dict(CTX, **ctx))
    assert rec["reason"] == reason and rec.get("tx") is None


def test_a_stable_payment_is_converted_at_the_eth_price_and_cut_by_the_fee():
    tape = _tape([_row(ZPOOL, B - 1, "0xpre", ZUP_PRE["sqrt"])])
    rec = fast.decide(dict(SIG, payment_token=USDG, paid=167_482_628), tape=tape, **CTX)
    # 167.482628 USDG at 2500 USD/ETH, less 1%: what the source's pool is assumed to have seen.
    assert rec["src_in_wei"] == 167_482_628 * 10 ** 12 * 99 // 100 // 2500
    assert rec["reason"] == "would_send"


def test_the_source_size_is_what_the_wallet_paid_in_usd_gross_of_fees():
    # The on-chain watcher's basis for the same buy (direct_buy_from_receipt: spent x ETH/USD), which
    # the funded hook's source-buy floor and band judge; the 1% haircut is for the pool, not this.
    tape = _tape([_row(ZPOOL, B - 1, "0xpre", ZUP_PRE["sqrt"])])
    eth = fast.decide(dict(SIG, paid=4 * 10 ** 16), tape=tape, **CTX)
    assert eth["paid_usd"] == pytest.approx(100.0)                  # 0.04 WETH at 2500
    usd = fast.decide(dict(SIG, payment_token=USDG, paid=167_482_628), tape=tape, **CTX)
    assert usd["paid_usd"] == pytest.approx(167.482628)


# ---- wiring: the tape tail, the float poll, one signal end to end, the read-only guard ----

import sqlite3  # noqa: E402
from pathlib import Path  # noqa: E402


def _tape_db(path, rows):
    db = sqlite3.connect(path)
    db.executescript(
        "CREATE TABLE pool_ticks (id INTEGER PRIMARY KEY, chain_id TEXT, pool TEXT, kind TEXT,"
        " block_no INTEGER, tx_sig TEXT, log_index INTEGER, maker TEXT, amount0 TEXT, amount1 TEXT,"
        " observed_at TEXT, tick INTEGER, liquidity TEXT, sqrt_price_x96 TEXT);"
        "CREATE TABLE pool_meta (chain_id TEXT, pool TEXT, token0 TEXT, token1 TEXT, quote_side INTEGER,"
        " resolved_at TEXT, token0_decimals INTEGER, token1_decimals INTEGER, resolve_state TEXT,"
        " fee INTEGER, tick_spacing INTEGER, hooks TEXT);"
        "CREATE TABLE onchain_pool_protocols (chain_id TEXT, pool TEXT, protocol TEXT, mint TEXT);"
        "CREATE TABLE volume_quote_prices (chain_id TEXT, token TEXT, usd REAL, observed REAL);"
        "CREATE TABLE volume_token_decimals (chain_id TEXT, token TEXT, decimals INTEGER, observed REAL);")
    db.execute("INSERT INTO pool_meta VALUES ('4663',?,?,?,0,'x',18,18,'ok',0,200,?)",
               (ZPOOL, NATIVE, TOKEN, HOOK))
    db.execute("INSERT INTO onchain_pool_protocols VALUES ('4663',?,'v4',?)", (ZPOOL, TOKEN))
    db.executemany("INSERT INTO pool_ticks (chain_id,pool,kind,block_no,tx_sig,log_index,amount0,"
                   "amount1,observed_at,liquidity,sqrt_price_x96) VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    db.commit()
    return db


def _tick(block, tx, sqrt, chain="4663", kind="v3_swap", pool=ZPOOL, t="2026-10-01T21:00:00+00:00"):
    return (chain, pool, kind, block, tx, 0, "1", "-1", t, str(ZUP_PRE["liquidity"]), str(sqrt))


@pytest.mark.parametrize("first_result,duration,pause", [("ok", 13, 0), ("ok", 2, 8),
                                                         ("error", 13, 10),
                                                         ("rate_limit", 13, 20), ("raise", 13, 10)])
def test_float_worker_schedules_healthy_reads_from_start_but_keeps_failure_backoff(
        tmp_path, monkeypatch, first_result, duration, pause):
    import threading
    import time
    from sniper.settings import memecoin as cfg
    from sniper.services import memecoin_rh_fast_lane as lane
    from sniper.util import clock
    db = _tape_db(tmp_path / "t.db", [])
    db.execute("INSERT INTO volume_quote_prices VALUES ('4663',?,2500,1000)", (NATIVE,))
    db.commit()
    db.close()
    now, starts, sleeps, published = [1000.0], [], [], []
    monkeypatch.setenv("MEMECOIN_RESEARCH_QUICKNODE_RPC_URL_4663", "https://research.example")
    monkeypatch.setattr(cfg, "MEMECOIN_OWN_ADDRESSES", TAKER)
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    monkeypatch.setattr(time, "time", lambda: now[0])
    monkeypatch.setattr(clock, "real_utc_now", lambda: now[0])
    monkeypatch.setattr(threading.Thread, "start", lambda self: None)
    monkeypatch.setattr(lane, "make_lane_sender", lambda *a: None)
    monkeypatch.setattr(fast, "_research_rpc", lambda *a, **kw: object())
    monkeypatch.setattr(fast, "_beat", lambda *a, **kw: None)

    def poll(*args):
        if len(starts) == 2:
            raise KeyboardInterrupt
        starts.append(now[0])
        now[0] += duration
        kind = first_result if len(starts) == 1 else "ok"
        if kind == "raise":
            raise OSError("provider unavailable")
        if kind != "ok":
            return None, kind
        return dict(native_wei=0, weth_wei=0, nonce=0, gas_price_wei=1,
                    read_at=starts[-1]), None

    tick = fast.float_tick
    def capture(*a, **kw):
        out = tick(*a, **kw)
        if out.get("snapshot") is not None:
            published.append((now[0], out["snapshot"]["read_at"]))
        return out
    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds
    monkeypatch.setattr(fast, "poll_snapshot", poll)
    monkeypatch.setattr(fast, "float_tick", capture)
    monkeypatch.setattr(time, "sleep", sleep)
    _, threads = fast.start_decide(db_path=str(tmp_path / "d.db"),
                                   tape_path=str(tmp_path / "t.db"), log=lambda *a: None)
    worker = next(t for t in threads if t.name == "rh-fast-float")
    with pytest.raises(KeyboardInterrupt):
        worker._target()
    assert starts == [1000, 1000 + duration + pause]
    assert sleeps == [pause, max(0, 10 - duration)]
    if first_result == "ok":
        assert published[1][0] - published[0][1] < 30  # previous snapshot stays inside the TTL


def test_the_tail_ingests_only_4663_swaps_and_advances_its_cursor(tmp_path):
    _tape_db(tmp_path / "t.db", [_tick(B - 2, "0xa", 11), _tick(B - 1, "0xb", 12, chain="56"),
                                 _tick(B - 1, "0xc", 13, kind="v3_mint"), _tick(B - 1, "0xd", 14)])
    conn = fast.open_tape(str(tmp_path / "t.db"))
    tape = fast.TapeState(fast.meta_reader(conn))
    cursor = fast.tail_once(conn, tape, 0)
    assert cursor == 4
    kind, row = tape.state(ZPOOL, B, "0xnone")
    assert kind == "pre" and (row["tx_sig"], row["sqrt"], row["liquidity"]) == (
        "0xd", 14, ZUP_PRE["liquidity"])
    assert fast.tail_once(conn, tape, cursor) == cursor               # nothing new


def test_an_unresolved_pool_has_no_meta(tmp_path):
    db = _tape_db(tmp_path / "t.db", [])
    db.execute("UPDATE pool_meta SET hooks=NULL")
    db.commit()
    assert fast.meta_reader(fast.open_tape(str(tmp_path / "t.db")))(ZPOOL) is None


def test_the_snapshot_poll_reads_balances_nonce_and_gas_and_types_a_rate_limit():
    # M2 step 2: the runner reads nothing before its send, so the detector's research-host poll
    # carries the wallet's native and WETH balances, its pending nonce and the gas price, stamped
    # with the HOST time the poll STARTED (the execution generation is host-clock too).
    calls = []
    weth = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"

    def rpc(method, params):
        calls.append((method, params))
        return {"eth_getBalance": ("0x" + "de0b6b3a7640000", None),
                "eth_call": ("0x" + "%064x" % (5 * 10 ** 15), None),
                "eth_getTransactionCount": ("0x2a", None),
                "eth_gasPrice": ("0x2625a00", None)}[method]
    snap, error = fast.poll_snapshot(rpc, "0xown", weth, clock=lambda: 123.5)
    assert error is None and snap == dict(native_wei=10 ** 18, weth_wei=5 * 10 ** 15, nonce=42,
                                          gas_price_wei=40_000_000, read_at=123.5)
    assert [c[0] for c in calls] == ["eth_getBalance", "eth_call", "eth_getTransactionCount",
                                     "eth_gasPrice"]
    assert calls[1][1][0] == {"to": weth, "data": "0x70a08231" + "00" * 12 + "0xown"[2:].rjust(40, "0")}
    assert calls[2][1] == ["0xown", "pending"]
    assert fast.poll_snapshot(lambda m, p: (None, {"http": 429}), "0xown", weth) == (None, "rate_limit")
    assert fast.poll_snapshot(lambda m, p: (None, {"transport": "Timeout"}), "0xown", weth)[1] == "error"
    assert fast.poll_snapshot(rpc, None, weth) == (None, "no_address")


def test_one_signal_is_decided_timed_and_written(tmp_path):
    import time
    tape = _tape([_row(ZPOOL, B - 3, "0xpre", ZUP_PRE["sqrt"])])
    db = str(tmp_path / "shadow.db")
    live = dict(CTX, now=None)
    rec = dict(SIG, paid=_paid_for(ZUP_IN), handle="Degen", arrival_utc="2026-10-01T21:00:00+00:00")
    out = fast.handle_signal(rec, time.perf_counter(), tape=tape, live=live, db_path=db,
                             now_fn=lambda: 1000.0)
    assert out["reason"] == "would_send" and out["decide_ms"] >= 0 and out["handoff_ms"] >= 0
    row = sqlite3.connect(db).execute(
        "SELECT tx_hash, reason, pool, state_kind, min_out, tx_bytes, activity_rank "
        "FROM rh_fast_decisions").fetchone()
    assert row[:4] == ("0xsrc", "would_send", ZPOOL, "pre")
    assert int(row[4]) == out["min_out"] and row[5] > 0 and row[6] == 1


def test_a_decide_that_raises_is_a_named_fallback_and_still_written(tmp_path):
    import time
    db = str(tmp_path / "shadow.db")

    class Broken:
        def route(self, *_a):
            raise RuntimeError("boom")
    out = fast.handle_signal(dict(SIG, paid=1), time.perf_counter(), tape=Broken(),
                             live=dict(CTX, now=None), db_path=db, now_fn=lambda: 1000.0)
    assert out["reason"] == "decide_error"
    assert sqlite3.connect(db).execute("SELECT reason FROM rh_fast_decisions").fetchone() == (
        "decide_error",)


def test_the_true_source_swap_is_written_onto_the_decision_later(tmp_path):
    import time
    db = str(tmp_path / "shadow.db")
    tape = _tape([_row(ZPOOL, B - 3, "0xpre", ZUP_PRE["sqrt"])])
    fast.handle_signal(dict(SIG, paid=_paid_for(ZUP_IN)), time.perf_counter(), tape=tape,
                       live=dict(CTX, now=None), db_path=db, now_fn=lambda: 1000.0)
    t = _tape_db(tmp_path / "t.db", [])
    assert fast.enrich_true(db_path=db, tape_path=str(tmp_path / "t.db")) == 0
    t.execute("INSERT INTO pool_ticks (chain_id,pool,kind,block_no,tx_sig,log_index,amount0,amount1,"
              "observed_at,liquidity,sqrt_price_x96) VALUES ('4663',?,'v3_swap',?,'0xsrc',3,?,?,'x',"
              "'1',?)", (ZPOOL, B, str(ZUP_IN), str(-ZUP_OUT), str(ZUP_POST)))
    # A routed source also swaps elsewhere in the same tx (a later log): our pool's row wins.
    t.execute("INSERT INTO pool_ticks (chain_id,pool,kind,block_no,tx_sig,log_index,amount0,amount1,"
              "observed_at,liquidity,sqrt_price_x96) VALUES ('4663',?,'v3_swap',?,'0xsrc',9,'5','-5',"
              "'x','1','77')", (PPOOL, B))
    t.commit()
    assert fast.enrich_true(db_path=db, tape_path=str(tmp_path / "t.db")) == 1
    assert sqlite3.connect(db).execute(
        "SELECT true_checked, true_pool, true_post_sqrt, true_in_wei, true_out, true_liquidity, "
        "state_liquidity, lp_fee_pips FROM rh_fast_decisions").fetchone() == (
        1, ZPOOL, str(ZUP_POST), str(ZUP_IN), str(ZUP_OUT), "1", str(ZUP_PRE["liquidity"]), 0)


def test_the_shadow_never_signs_sends_reserves_or_reads_a_funded_host():
    src = Path(fast.__file__).read_text()
    for name in ("sign_transaction", "eth_sendRawTransaction", "reserve_entry", "private_key",
                 "MEMECOIN_EVM_RPC", "custody", "_freshest_read_member"):
        assert name not in src, name
    assert "MEMECOIN_RESEARCH_QUICKNODE_RPC_URL_4663" in src


def test_only_signals_reach_the_decide_thread_and_every_record_reaches_the_writer():
    from sniper.services.memecoin_rh_feed_shadow import make_sink
    written, decided = [], []
    sink = make_sink(written.append, decided.append)
    sink({"signal": True, "tx_hash": "0x1"})
    sink({"signal": False, "tx_hash": "0x2"})
    assert [r["tx_hash"] for r in written] == ["0x1", "0x2"]
    assert [r["tx_hash"] for r in decided] == ["0x1"]
    make_sink(written.append, None)({"signal": True, "tx_hash": "0x3"})     # M1 off: writer only
    assert written[-1]["tx_hash"] == "0x3" and len(decided) == 1


def test_the_decide_shadow_is_off_unless_named(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.delenv("MEMECOIN_RH_FAST_DECIDE_SHADOW", raising=False)
    assert cfg.memecoin_rh_fast_decide_shadow() is False
    for raw, on in (("1", True), ("on", True), ("0", False), ("live", False)):
        monkeypatch.setenv("MEMECOIN_RH_FAST_DECIDE_SHADOW", raw)
        assert cfg.memecoin_rh_fast_decide_shadow() is on


def test_the_tail_walks_the_rowid_never_the_chain_index(tmp_path):
    # With the tape's (chain_id, pool, kind, observed_at) index, SQLite chose it for the tail and
    # sorted every 4663 row on each 100 ms poll: 8.5% of a core on the funded host (measured
    # 2026-10-01). The tail must range-scan the rowid after its cursor.
    db = _tape_db(tmp_path / "t.db", [])
    db.execute("CREATE INDEX idx_pool_ticks_pool_kind_time ON pool_ticks(chain_id, pool, kind, observed_at)")
    db.commit()
    seen = []
    conn = fast.open_tape(str(tmp_path / "t.db"))
    conn.set_trace_callback(seen.append)
    fast.tail_once(conn, fast.TapeState(META.get), 0)
    (sql,) = [s for s in seen if "FROM pool_ticks" in s]
    plan = " ".join(r[-1] for r in conn.execute("EXPLAIN QUERY PLAN " + sql))
    assert "INTEGER PRIMARY KEY" in plan and "idx_" not in plan, plan


# ---- Gamma's review of 1add9c5b0 ----

def test_the_pool_lp_fee_comes_off_the_source_input_before_the_swap_step():
    # Gamma LOW: swap_native_in takes what the pool keeps after fees; decide passed the gross.
    paid = _paid_for(DOTTIE_IN)                       # haircut leaves exactly DOTTIE_IN (gross)
    tape = _tape([_row(PPOOL, B - 1, "0xpre", DOTTIE_PRE["sqrt"], liquidity=DOTTIE_PRE["liquidity"])])
    rec = fast.decide(dict(SIG, paid=paid), tape=tape, **CTX)
    net = DOTTIE_IN * (10 ** 6 - 10000) // 10 ** 6   # LP 1%; the 0.1% protocol fee is not known
    assert rec["src_in_wei"] == DOTTIE_IN and rec["lp_fee_pips"] == 10000
    assert rec["est_post_sqrt"] == fast.swap_native_in(
        DOTTIE_PRE["sqrt"], DOTTIE_PRE["liquidity"], net)[0]


def test_a_dynamic_fee_pool_deducts_no_lp_fee():
    meta = dict(META, **{PPOOL: dict(META[PPOOL], fee=0x800000, hooks=HOOK)})
    tape = fast.TapeState(meta.get)
    tape.ingest([_row(PPOOL, B - 1, "0xpre", DOTTIE_PRE["sqrt"], liquidity=DOTTIE_PRE["liquidity"])])
    rec = fast.decide(dict(SIG, paid=_paid_for(DOTTIE_IN)), tape=tape, **CTX)
    assert rec["lp_fee_pips"] == 0 and rec["est_post_sqrt"] == fast.swap_native_in(
        DOTTIE_PRE["sqrt"], DOTTIE_PRE["liquidity"], DOTTIE_IN)[0]


def test_the_decision_records_the_state_liquidity():
    tape = _tape([_row(ZPOOL, B - 3, "0xpre", ZUP_PRE["sqrt"])])
    rec = fast.decide(dict(SIG, paid=_paid_for(ZUP_IN)), tape=tape, **CTX)
    assert rec["state_liquidity"] == ZUP_PRE["liquidity"]


SNAP = dict(native_wei=7, weth_wei=3, nonce=4, gas_price_wei=9, read_at=999.0)


@pytest.mark.parametrize("bad", ["not-hex", {}, -1, "0x-1"])
def test_malformed_snapshot_is_absent_and_can_expire_the_previous_float(bad):
    snap, error = fast.poll_snapshot(lambda method, params: (bad, None), TAKER, TOKEN)
    assert snap is None and error == "error"
    tick = fast.float_tick((snap, error), (2500, 1000), now=1061, ok_at=1000, period=10)
    assert tick["float_wei"] is None and tick["snapshot"] is None


def test_float_rpc_uses_account_pacing_on_the_drpc_fallback(monkeypatch):
    import collections
    import io
    import json
    from sniper.execution import research_rotation_4663 as rr
    from sniper.services import memecoin_research_pacer as pacer
    drpc = "https://lb.drpc.org/ogrpc?network=robinhood&dkey=SECRET"
    rot = rr.Rotation({rr.BASE: drpc}, log=lambda _: None)
    reservations = []
    monkeypatch.setattr(pacer.SharedPacer, "acquire", lambda self: reservations.append(self.path))
    monkeypatch.setattr("urllib.request.urlopen", lambda req, **k: _SnapshotResponse(req))

    class _SnapshotResponse(io.BytesIO):
        status = 200

        def __init__(self, req):
            payload = json.loads(req.data)
            super().__init__(json.dumps([{"id": row["id"], "result": "0x1"}
                                        for row in payload]).encode())

    rpc = fast._research_rpc(drpc, collections.Counter(), spacing_s=0, rotation=rot)
    assert fast.poll_snapshot(rpc, TAKER, TOKEN)[1] is None
    assert len(reservations) == 2


@pytest.mark.parametrize("reply", [[], {"error": "broken"}, {"result": None}, {"result": "not-hex"}])
def test_malformed_rpc_reply_reaches_snapshot_expiry_without_raising(monkeypatch, reply):
    import collections
    class Rotation:
        def send(self, url, do, **kwargs):
            return 200, reply
    rpc = fast._research_rpc("https://research.example", collections.Counter(), spacing_s=0,
                             rotation=Rotation())
    assert fast.poll_snapshot(rpc, TAKER, TOKEN) == (None, "error")


@pytest.mark.parametrize("result, usd_row, now, ok_at, period, want", [
    # a good read sets the float and the snapshot, resets the period and stamps ok_at
    ((SNAP, None), (2500.0, 1000.0), 1000.0, 0.0, 40.0,
     dict(float_wei=7, gas_price_wei=9, snapshot=SNAP, eth_usd=2500.0, eth_usd_at=1000.0,
          ok_at=1000.0, period=10.0)),
    # a 429 doubles the period up to 60 s and keeps the last good float while it is fresh
    ((None, "rate_limit"), (2500.0, 1000.0), 1030.0, 1000.0, 40.0,
     dict(eth_usd=2500.0, eth_usd_at=1000.0, ok_at=1000.0, period=60.0)),
    ((None, "rate_limit"), (2500.0, 1000.0), 1030.0, 1000.0, 10.0,
     dict(eth_usd=2500.0, eth_usd_at=1000.0, ok_at=1000.0, period=20.0)),
    # no good read for more than 60 s: float and snapshot are absent, never the last value
    ((None, "error"), (2500.0, 1000.0), 1061.0, 1000.0, 10.0,
     dict(float_wei=None, gas_price_wei=None, snapshot=None, eth_usd=2500.0, eth_usd_at=1000.0,
          ok_at=1000.0, period=10.0)),
    # an ETH price older than 600 s is absent; exactly 600 s still counts
    ((SNAP, None), (2500.0, 1000.0), 1601.0, 0.0, 10.0,
     dict(float_wei=7, gas_price_wei=9, snapshot=SNAP, eth_usd=None, eth_usd_at=None,
          ok_at=1601.0, period=10.0)),
    ((SNAP, None), (2500.0, 1000.0), 1600.0, 0.0, 10.0,
     dict(float_wei=7, gas_price_wei=9, snapshot=SNAP, eth_usd=2500.0, eth_usd_at=1000.0,
          ok_at=1600.0, period=10.0)),
    ((SNAP, None), None, 1000.0, 0.0, 10.0,
     dict(float_wei=7, gas_price_wei=9, snapshot=SNAP, eth_usd=None, eth_usd_at=None,
          ok_at=1000.0, period=10.0)),
])
def test_one_float_tick(result, usd_row, now, ok_at, period, want):
    assert fast.float_tick(result, usd_row, now=now, ok_at=ok_at, period=period) == want


def test_m1_threads_never_take_m0_down():
    # Gamma LOW: M0's loop exits on any dead thread in shadow.threads; M0 is under grade, so M1's
    # threads are kept out of that set and report through rh_fast_heartbeat.threads_alive.
    from sniper.services import memecoin_rh_feed_shadow as m0
    src = Path(m0.__file__).read_text()
    start = src[src.index("def start_shadow("):src.index("def process_frame(")]
    assert "shadow.threads +=" not in start and "shadow.threads.extend" not in start
    assert "shadow.decide_threads = decide_threads" in start


# ---- M2 step 2b: the detector names the mints the runner's safety cache should hold ----

def test_candidates_put_signal_and_armed_mints_first_then_the_busiest_routes():
    meta = dict(META, **{
        "0x" + "d4" * 32: dict(protocol="v4", mint="0xbusy", token0=NATIVE, token1="0xbusy", fee=0,
                               tick_spacing=200, hooks=HOOK),
        "0x" + "e5" * 32: dict(protocol="v4", mint="0xquiet", token0=NATIVE, token1="0xquiet",
                               fee=0, tick_spacing=200, hooks=HOOK)})
    tape = fast.TapeState(meta.get)
    rows = ([_row("0x" + "d4" * 32, B - 1, "0xb%d" % i, 1) for i in range(5)]
            + [_row("0x" + "e5" * 32, B - 1, "0xq", 1), _row(ZPOOL, B - 1, "0xz", 1),
               _row(WPOOL, B - 1, "0xw", 1)])
    tape.ingest(rows)
    entries = fast.make_candidates(tape, 1000.0, signal_mints={TOKEN}, armed_mints={"0xnoroute"},
                                   k=2)
    assert [(e["mint"], e["priority"]) for e in entries] == [(TOKEN, 0), ("0xbusy", 11), ("0xquiet", 12)]
    assert entries[0]["pool"] == ZPOOL and entries[0]["key"]["hooks"] == HOOK


def test_armed_bought_mints_are_the_last_hours_4663_decisions(tmp_path):
    db = sqlite3.connect(tmp_path / "ledger.db")
    db.executescript("CREATE TABLE memecoin_entry_attempts (attempt_id TEXT, completed_at TEXT, "
                     "network_id TEXT, mint TEXT);"
                     "CREATE INDEX idx_mc_entry_view_completed ON memecoin_entry_attempts(completed_at);")
    db.executemany("INSERT INTO memecoin_entry_attempts VALUES (?,?,?,?)", [
        ("a", "2026-10-01T21:30:00+00:00", "4663", "0xAA"),
        ("b", "2026-10-01T20:00:00+00:00", "4663", "0xbb"),            # older than the hour
        ("c", "2026-10-01T21:40:00+00:00", "56", "0xcc"),              # another chain
        ("d", "2026-10-01T21:45:00+00:00", "4663", None)])
    db.commit()
    from sniper.persistence.db_memecoin import armed_bought_mints
    assert armed_bought_mints(str(tmp_path / "ledger.db"), "4663", "2026-10-01T21:00:00+00:00") == {"0xaa"}


def test_a_native_pool_belongs_to_its_coin_even_with_a_native_protocol_row_first(tmp_path):
    # Found in the M2 scratch run (2026-10-01): onchain_pool_protocols holds one row PER SIDE of a
    # V4 pool (66,321 of 314,422 4663 v4 rows name the native 0x0 as the mint), and the meta join
    # took the first row, so most native pools were filed under 0x0 and their coins had no route.
    db = _tape_db(tmp_path / "t.db", [])
    db.execute("DELETE FROM onchain_pool_protocols")
    db.execute("INSERT INTO onchain_pool_protocols VALUES ('4663',?,'v4',?)", (ZPOOL, NATIVE))
    db.execute("INSERT INTO onchain_pool_protocols VALUES ('4663',?,'v4',?)", (ZPOOL, TOKEN))
    db.commit()
    meta = fast.meta_reader(fast.open_tape(str(tmp_path / "t.db")))(ZPOOL)
    assert meta["mint"] == TOKEN and meta["protocol"] == "v4"


@pytest.mark.parametrize("token0, token1, coin", [
    (NATIVE, TOKEN, TOKEN),
    (TOKEN, WETH, TOKEN),          # Gamma LOW: a coin/WETH pool is the coin's, not WETH's
    (TOKEN, USDG, TOKEN),
    (WETH, TOKEN, TOKEN),
])
def test_a_pool_belongs_to_the_side_that_is_not_a_quote_asset(tmp_path, token0, token1, coin):
    db = _tape_db(tmp_path / "t.db", [])
    db.execute("UPDATE pool_meta SET token0=?, token1=?", (token0, token1))
    db.commit()
    assert fast.meta_reader(fast.open_tape(str(tmp_path / "t.db")))(ZPOOL)["mint"] == coin


# ---- Gamma 2026-10-01: the tape's pool_meta key is blank for 69% of native 4663 pools ----

VERIFIED = dict(currency0=NATIVE, currency1=TOKEN, fee=0, tick_spacing=200, hooks=HOOK)


def test_an_incomplete_tape_key_falls_back_to_the_ledgers_verified_key(tmp_path):
    db = _tape_db(tmp_path / "t.db", [])
    db.execute("UPDATE pool_meta SET fee=NULL, tick_spacing=NULL, hooks=NULL")
    db.commit()
    asked = []
    meta = fast.meta_reader(fast.open_tape(str(tmp_path / "t.db")),
                            key_fn=lambda pool: asked.append(pool) or dict(VERIFIED))(ZPOOL)
    assert asked == [ZPOOL]
    assert (meta["protocol"], meta["mint"], meta["token0"], meta["token1"]) == ("v4", TOKEN, NATIVE, TOKEN)
    assert (meta["fee"], meta["tick_spacing"], meta["hooks"]) == (0, 200, HOOK)


def test_a_pool_the_tape_never_resolved_routes_on_its_verified_key(tmp_path):
    db = _tape_db(tmp_path / "t.db", [_tick(B - 1, "0xa", ZUP_PRE["sqrt"])])
    db.execute("DELETE FROM pool_meta")
    db.execute("DELETE FROM onchain_pool_protocols")
    db.commit()
    conn = fast.open_tape(str(tmp_path / "t.db"))
    tape = fast.TapeState(fast.meta_reader(conn, key_fn=lambda pool: dict(VERIFIED)))
    fast.tail_once(conn, tape, 0)
    pool, key = tape.route(TOKEN, now=_epoch_of("2026-10-01T21:00:00+00:00"))
    assert pool == ZPOOL and key == dict(VERIFIED, currency1=TOKEN.lower(), hooks=HOOK.lower())


def _epoch_of(iso):
    from datetime import datetime
    return datetime.fromisoformat(iso).timestamp()


def test_without_a_verified_key_an_incomplete_pool_stays_unresolved(tmp_path):
    db = _tape_db(tmp_path / "t.db", [])
    db.execute("UPDATE pool_meta SET hooks=NULL")
    db.commit()
    assert fast.meta_reader(fast.open_tape(str(tmp_path / "t.db")),
                            key_fn=lambda pool: None)(ZPOOL) is None


def test_a_complete_tape_key_never_asks_the_ledger(tmp_path):
    _tape_db(tmp_path / "t.db", [])

    def key_fn(pool):
        raise AssertionError("a complete pool_meta key read the ledger")
    meta = fast.meta_reader(fast.open_tape(str(tmp_path / "t.db")), key_fn=key_fn)(ZPOOL)
    assert meta["hooks"] == HOOK and meta["mint"] == TOKEN


def test_the_ledger_key_reader_reads_memecoin_evm_v4_pool_keys_read_only(tmp_path):
    from sniper.persistence import db_memecoin as m
    from sniper.services.memecoin_evm_v4_events import V4_POOL_MANAGERS
    path = str(tmp_path / "ledger.db")
    ledger = sqlite3.connect(path)
    m.ensure_memecoin_tables(ledger)
    m.put_evm_v4_pool_key(ledger, chain_id="4663", pool_id=ZPOOL,
                          pool_manager=V4_POOL_MANAGERS["4663"], key=dict(VERIFIED))
    ledger.commit()
    read = fast.ledger_key_reader(path)
    assert read(ZPOOL) == VERIFIED
    assert read("0x" + "ee" * 32) is None
    with pytest.raises(sqlite3.OperationalError):          # the handle can never write
        read.conn.execute("DELETE FROM memecoin_evm_v4_pool_keys")


def test_the_detector_resolves_a_blank_tape_key_from_the_ledger(tmp_path, monkeypatch):
    import threading
    from sniper.persistence import db_memecoin as m
    from sniper.services.memecoin_evm_v4_events import V4_POOL_MANAGERS
    db = _tape_db(tmp_path / "t.db", [])
    db.execute("UPDATE pool_meta SET fee=NULL, tick_spacing=NULL, hooks=NULL")
    db.commit()
    ledger_path = str(tmp_path / "ledger.db")
    ledger = sqlite3.connect(ledger_path)
    m.ensure_memecoin_tables(ledger)
    m.put_evm_v4_pool_key(ledger, chain_id="4663", pool_id=ZPOOL,
                          pool_manager=V4_POOL_MANAGERS["4663"], key=dict(VERIFIED))
    ledger.commit()
    captured = {}

    class Capture(fast.TapeState):
        def __init__(self, meta_fn):
            captured["meta_fn"] = meta_fn
            super().__init__(meta_fn)
    monkeypatch.setattr(fast, "TapeState", Capture)
    monkeypatch.setattr(threading.Thread, "start", lambda self: None)   # wiring only, no loops
    fast.start_decide(db_path=str(tmp_path / "d.db"), tape_path=str(tmp_path / "t.db"),
                      log=lambda *a: None, ledger_path=ledger_path)
    meta = captured["meta_fn"](ZPOOL)
    assert meta and (meta["fee"], meta["tick_spacing"], meta["hooks"]) == (0, 200, HOOK)


# ---- Gamma LOW on c1363cab3: a ledger that keeps failing must not log at tape rate ----

def _failing_reader(tmp_path, monkeypatch, fail):
    from sniper.persistence import db_memecoin as m
    path = str(tmp_path / "ledger.db")
    ledger = sqlite3.connect(path)
    m.ensure_memecoin_tables(ledger)
    ledger.commit()
    calls, now = [], [1000.0]
    real = m.get_evm_v4_pool_key

    def get(conn, **k):
        calls.append(k["pool_id"])
        if fail[0]:
            raise sqlite3.OperationalError("disk I/O error")
        return real(conn, **k)
    monkeypatch.setattr(m, "get_evm_v4_pool_key", get)
    return fast.ledger_key_reader(path, clock=lambda: now[0]), calls, now


def test_five_failed_reads_in_a_row_pause_the_ledger_for_a_minute(tmp_path, monkeypatch, caplog):
    import logging
    caplog.set_level(logging.DEBUG, logger=fast.__name__)
    fail = [True]
    read, calls, now = _failing_reader(tmp_path, monkeypatch, fail)
    for i in range(8):
        assert read("0x%064x" % i) is None
    assert len(calls) == 5 and read.counts["errors"] == 5 and read.counts["skipped"] == 3
    loud = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(loud) == 1 and "60" in loud[0].getMessage()
    now[0] += 61.0
    fail[0] = False
    assert read("0x%064x" % 99) is None and len(calls) == 6        # asks again: a miss now
    assert read.counts["misses"] == 1


def test_a_read_that_works_resets_the_failure_count(tmp_path, monkeypatch):
    fail = [True]
    read, calls, now = _failing_reader(tmp_path, monkeypatch, fail)
    for i in range(4):
        read("0x%064x" % i)
    fail[0] = False
    read("0x%064x" % 50)                                            # a miss is a working read
    fail[0] = True
    for i in range(4):
        read("0x%064x" % (60 + i))
    assert len(calls) == 9 and read.counts["skipped"] == 0


# ---- Alpha 2026-10-02 B1: in live the feed forwards would_send only, so its float check must use
# the lane's clip; a $170 check would stop every $50 lane signal as float_short ----

@pytest.mark.parametrize("mode, clip", [("live", 50.0), ("runner_shadow", 170.0), ("", 170.0)])
def test_the_production_feed_checks_the_float_at_the_lanes_clip_only_while_live(
        tmp_path, monkeypatch, mode, clip):
    import threading
    from sniper.settings import memecoin as cfg
    _tape_db(tmp_path / "t.db", []).commit()
    monkeypatch.setenv("MEMECOIN_RH_FAST_LANE", mode)
    monkeypatch.setattr(cfg, "evm_max_clip_usd_for", lambda chain, **kw: 170.0)
    monkeypatch.setattr(cfg, "MEMECOIN_RH_FAST_LANE_PILOT_CLIP_USD", 50.0)
    monkeypatch.setattr(threading.Thread, "start", lambda self: None)   # wiring only, no loops
    seen = []

    def capture(rec, enqueued, *, live, **kw):
        seen.append(live["clip_usd"])
        raise KeyboardInterrupt                       # one pass of the real decide loop, then out
    monkeypatch.setattr(fast, "handle_signal", capture)
    offer, threads = fast.start_decide(db_path=str(tmp_path / "d.db"),
                                       tape_path=str(tmp_path / "t.db"), log=lambda *a: None)
    offer({"mint": "0x" + "33" * 20, "tx_hash": "0x" + "11" * 32})
    decide_loop = next(t for t in threads if t.name == "rh-fast-decide")
    with pytest.raises(KeyboardInterrupt):
        decide_loop._target()
    assert seen == [clip]


def test_a_pilot_clip_above_the_max_clip_keeps_the_max_clip(monkeypatch):
    # Gamma on 95ef1be56: live checks the SMALLER of the two, never the pilot clip alone.
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "evm_max_clip_usd_for", lambda chain, **kw: 170.0)
    monkeypatch.setattr(cfg, "MEMECOIN_RH_FAST_LANE_PILOT_CLIP_USD", 250.0)
    assert fast.feed_clip_usd("live") == 170.0
    monkeypatch.setattr(cfg, "MEMECOIN_RH_FAST_LANE_PILOT_CLIP_USD", 50.0)
    assert fast.feed_clip_usd("live") == 50.0 and fast.feed_clip_usd("runner_shadow") == 170.0


def test_latest_same_pool_is_diagnostic_and_never_replaces_source_limits():
    source = _row(ZPOOL, B, "0xsrc", ZUP_POST, a0=ZUP_IN, a1=-ZUP_OUT)
    original = fast.decide(dict(SIG, paid=1), tape=_tape([source]), **CTX)
    newer = _row(ZPOOL, B + 1, "0xlater", ZUP_POST // 2, t=1001.0, log=4)
    tape = _tape([source, newer, _row(PPOOL, B + 2, "0xforeign", 9)])
    changed = fast.decide(dict(SIG, paid=1), tape=tape, **CTX)
    for key in ("est_post_sqrt", "src_in_wei", "src_out", "cap_price", "min_out", "tx"):
        assert changed[key] == original[key]
    diag = changed["latest_same_pool"]
    assert (diag["block_no"], diag["log_index"], diag["tx_sig"]) == (B + 1, 4, "0xlater")
    assert diag["basis"] == "background_same_pool_observation"
    assert changed.get("quote_price") is None
    assert source == _row(ZPOOL, B, "0xsrc", ZUP_POST, a0=ZUP_IN, a1=-ZUP_OUT)


@pytest.mark.parametrize('damage,reason', [
    ({'network_id': '56'}, 'wrong_chain'), ({'removed': True}, 'removed_state'),
    ({'liquidity': None}, 'missing_liquidity_or_price')])
def test_unusable_latest_state_is_unknown_and_cannot_replace_source(damage, reason):
    source = _row(ZPOOL, B, '0xsrc', ZUP_POST, a0=ZUP_IN, a1=-ZUP_OUT)
    later = dict(_row(ZPOOL, B + 1, '0xlater', 1), **damage)
    changed = fast.decide(dict(SIG, paid=1), tape=_tape([source, later]), **CTX)
    original = fast.decide(dict(SIG, paid=1), tape=_tape([source]), **CTX)
    assert changed['tx'] == original['tx']
    assert changed['latest_same_pool']['unknown_reason'] == reason
    assert changed['latest_same_pool']['quote_price'] is None
    assert changed['latest_same_pool']['estimate_coverage'] == 'tick_crossing_and_hook_effects_unverified'


def test_stale_latest_observation_retains_typed_unknown_diagnostic():
    rec = fast.decide(dict(SIG, paid=1), tape=_tape([_row(ZPOOL, B - 20, '0xold', 1)]), **CTX)
    assert rec['latest_same_pool']['unknown_reason'] == 'stale_state'
    assert rec['reason'] == 'tape_behind'
