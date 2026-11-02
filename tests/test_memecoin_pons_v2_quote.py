import json

import pytest

from eth_abi import decode
from eth_utils import keccak

from sniper.execution.evm_pons_v2_quote import (
    GMGN_ROUTER, dex_pons_v2_quote, pons_round_trip)


CHAIN = "4663"
WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
MINT = "0x5e2442f74d6d92964b4f09aad63eefaf01919283"
CURVE = "0x671a9e3afcb4912866785f6439c0f7737e0aca85"
TAKER = "0x1111111111111111111111111111111111111111"
ZERO = "0x" + "00" * 20


@pytest.fixture(autouse=True)
def _route_quote_workers_end_in_their_test():
    """The ladder returns on a hit and leaves its other rungs running; here they ran the real v4 pool
    scan (evm_wallet._rpc -> urlopen) inside the NEXT test on the same xdist worker (Gamma, -n 10).
    Each test now waits for the evm-route-quote workers it started, and fails if one outlives 10 s."""
    import threading
    before = set(threading.enumerate())
    yield
    mine = [t for t in threading.enumerate()
            if t not in before and t.name.startswith("evm-route-quote")]
    for t in mine:
        t.join(10.0)
    assert not [t.name for t in mine if t.is_alive()], "evm-route-quote worker outlived its test"


def _word(value):
    if isinstance(value, str):
        value = int(value, 16)
    return "0x" + int(value).to_bytes(32, "big").hex()


def _address(value):
    return "0x" + bytes.fromhex(value[2:].rjust(64, "0")).hex()


def _call(*, graduated=False, sellable=10**30, token=MINT, pair=ZERO):
    values = {
        keccak(text="curve()")[:4].hex(): _address(CURVE),
        keccak(text="token()")[:4].hex(): _address(token),
        keccak(text="pairToken()")[:4].hex(): _address(pair),
        keccak(text="getReserves()")[:4].hex(): "0x" + _word(2 * 10**18)[2:] + _word(10**27)[2:],
        keccak(text="feeBps()")[:4].hex(): _word(100),
        keccak(text="creatorTaxBps()")[:4].hex(): _word(200),
        keccak(text="graduated()")[:4].hex(): _word(graduated),
        keccak(text="sellableTokens()")[:4].hex(): _word(sellable),
    }

    def call(_to, data):
        return values.get(data[2:10])

    return call


def _route(quote):
    types = [
        "(uint8,address,address,address,uint24,int24,address,bytes,address,bytes32)[]",
        "address", "uint256", "uint256", "uint256",
    ]
    return decode(types, bytes.fromhex(quote["transaction"]["data"][10:]))


def test_pons_v2_buy_uses_exact_curve_math_and_native_router_route():
    amount = 10**16
    quote = dex_pons_v2_quote(
        CHAIN, WETH, MINT, amount, TAKER, call_fn=_call(),
        slippage_bps=1500, now_fn=lambda: 1_000_000,
    )

    router_net = amount - amount * 100 // 10_000
    curve_net = router_net - router_net * 100 // 10_000 - router_net * 200 // 10_000
    expected = curve_net * 10**27 // (2 * 10**18 + curve_net)
    routes, receiver, amount_in, min_out, deadline = _route(quote)

    assert int(quote["buyAmount"]) == expected
    assert int(quote["minBuyAmount"]) == expected * 8500 // 10_000
    assert quote["transaction"]["to"].lower() == GMGN_ROUTER
    assert quote["transaction"]["value"] == hex(amount)
    assert quote["issues"]["allowance"] is None
    assert quote["pre_transactions"][0]["data"].startswith("0x2e1a7d4d")
    from sniper.services.memecoin_evm_gas_topup import WETH_CALL_GAS_LIMIT
    assert quote["pre_transactions"][0]["gas"] == str(WETH_CALL_GAS_LIMIT)
    assert routes[0][0:4] == (27, ZERO, MINT, CURVE)
    assert int.from_bytes(routes[0][7], "big") == 1
    assert receiver == ZERO and amount_in == amount
    assert min_out == expected * 8500 // 10_000 and deadline == 1_000_600


def test_pons_v2_sell_routes_native_proceeds_back_to_weth():
    amount = 10**23
    quote = dex_pons_v2_quote(
        CHAIN, MINT, WETH, amount, TAKER, call_fn=_call(),
        slippage_bps=2500, now_fn=lambda: 1_000_000,
    )

    gross = amount * (2 * 10**18) // (10**27 + amount)
    curve_net = gross - gross * 100 // 10_000 - gross * 200 // 10_000
    expected = curve_net - curve_net * 100 // 10_000
    routes, _receiver, amount_in, min_out, _deadline = _route(quote)

    assert int(quote["buyAmount"]) == expected
    assert quote["transaction"]["value"] == "0x0"
    assert quote["issues"]["allowance"]["spender"].lower() == GMGN_ROUTER
    assert routes[0][0:4] == (27, MINT, WETH, CURVE)
    assert int.from_bytes(routes[0][7], "big") == 0
    assert amount_in == amount and min_out == expected * 7500 // 10_000


def test_pons_v2_quote_rejects_unverified_or_closed_curves():
    assert dex_pons_v2_quote(
        CHAIN, WETH, MINT, 10**16, TAKER, call_fn=_call(token=TAKER),
    ) is None
    assert dex_pons_v2_quote(
        CHAIN, WETH, MINT, 10**16, TAKER, call_fn=_call(graduated=True),
    ) is None
    assert dex_pons_v2_quote(
        CHAIN, WETH, MINT, 10**16, TAKER, call_fn=_call(sellable=1),
    ) is None


# --- why the rung declined, in the rung ledger (2026-09-24) --------------------------------------
#
# `pons_v2` reported `hit=False blind_reason=None reason=None` on 14 of 18 runs on 4663 since
# 09-23, so nothing could say whether the coin was not a Pons token at all or the curve had simply
# left the venue. Classified live on 2026-09-24 over the 7 distinct mints behind those misses
# (robinhood-rpc.publicnode.com, eth_call only): `curve()` answered on 7 of 7 and all 7 read
# `graduated=1` with `sellableTokens()=0`. The quote was right to decline every one; only the
# silence was wrong.
#
# These are REASONS, never blind reasons: a graduated curve is a decided fact about the venue, and
# calling it blindness would turn a correct refusal into `provider_*` and change how the runner
# gates on it. A read that RAISES still raises.

def test_a_graduated_curve_says_so_instead_of_declining_in_silence():
    miss = {}
    assert dex_pons_v2_quote(
        CHAIN, WETH, MINT, 10**16, TAKER, call_fn=_call(graduated=True), miss_out=miss) is None
    assert miss["reason"] == "curve_graduated", miss


def test_an_erc20_counter_is_not_graduation_and_needs_a_prepared_bridge():
    """VEKTORBUILD's live curve disproved the old pairToken==graduated assumption."""
    miss = {}
    assert dex_pons_v2_quote(
        CHAIN, WETH, MINT, 10**16, TAKER, call_fn=_call(pair=TAKER), miss_out=miss) is None
    assert miss["reason"] == "curve_counter_not_prepared", miss


def test_a_live_curve_too_thin_for_the_clip_names_both_numbers():
    """The case Alpha expected and the live sample did not contain: a curve still on its bonding
    phase whose remainder is under our clip. Both numbers travel with it so a reader can size the
    gap instead of re-deriving it."""
    miss = {}
    assert dex_pons_v2_quote(
        CHAIN, WETH, MINT, 10**16, TAKER, call_fn=_call(sellable=1), miss_out=miss) is None
    assert miss["reason"].startswith("curve_remainder_below_clip:"), miss
    assert "sellable=1" in miss["reason"] and "amount_out=" in miss["reason"], miss


def test_a_token_that_is_not_pons_stays_silent():
    """`curve()` answering nothing is not a Pons token, and 4663 is full of them. A reason here
    would turn every ordinary coin's ladder into noise."""
    miss = {}
    assert dex_pons_v2_quote(
        CHAIN, WETH, MINT, 10**16, TAKER, call_fn=lambda _to, _data: None, miss_out=miss) is None
    assert miss == {}, miss


def test_a_curve_whose_getters_answer_nothing_is_unreadable_not_absent():
    """`curve()` answered, so this IS a Pons token; a getter that then returns nothing is a state
    we could not read, which is a third thing from graduated and from not-Pons."""
    base = _call()

    def call(to, data):
        if data[2:10] == keccak(text="getReserves()")[:4].hex():
            return None
        return base(to, data)

    miss = {}
    assert dex_pons_v2_quote(CHAIN, WETH, MINT, 10**16, TAKER, call_fn=call, miss_out=miss) is None
    assert miss["reason"] == "curve_state_unreadable", miss


def test_a_raising_read_is_still_blindness_and_not_a_refusal():
    """The E5 boundary, restated here: an unreadable PROVIDER must not be recorded as a fact about
    the coin. `PonsReadUnavailable` still propagates."""
    from sniper.execution.evm_pons_v2_quote import PonsReadUnavailable

    def call(_to, _data):
        raise PonsReadUnavailable("rpc_transport", "boom")

    miss = {}
    with pytest.raises(PonsReadUnavailable):
        dex_pons_v2_quote(CHAIN, WETH, MINT, 10**16, TAKER, call_fn=call, miss_out=miss)

def test_pons_v2_route_fails_over_from_a_primary_rpc_transport_fault(monkeypatch):
    """A Pons route stays executable when the configured primary RPC fails."""
    from sniper.execution import evm_rpc_pool
    from sniper.execution.evm_real_seams import venue_quote_with_v2_fallback
    from sniper.settings import memecoin as cfg

    primary = "https://primary.example"
    secondary = "https://secondary.example"
    calls = _call()

    def http(_method, url, *, body=None, **_kwargs):
        payload = json.loads(body)
        if url == primary:
            raise OSError("primary transport fault")
        if payload["method"] == "eth_chainId":
            return 200, {"jsonrpc": "2.0", "id": 1, "result": "0x1237"}, None
        call = payload["params"][0]
        return 200, {
            "jsonrpc": "2.0", "id": 1,
            "result": calls(call["to"], call["data"]),
        }, None

    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RPC_POOL_URLS", {CHAIN: (primary, secondary)})
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_ENABLED", False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RELAY_ENABLED", False)
    evm_rpc_pool.reset_for_tests()
    try:
        quote = venue_quote_with_v2_fallback(
            lambda *_args, **_kwargs: None, chain=CHAIN, quote_token=WETH, taker=TAKER,
            http=http, v2_quote_fn=lambda *_args, **_kwargs: None,
            pons_v2_quote_fn=dex_pons_v2_quote, rpc_url=primary,
        )(WETH, MINT, 10**16, slippage_bps=1500)
        assert quote["route"]["protocol"] == "pons_v2"
    finally:
        evm_rpc_pool.reset_for_tests()


# --- entry cost gate (2026-09-05) --------------------------------------------------------------
# The rung admitted a funded entry with no measure of what a clip costs to buy and sell back.
# Funded Pons history to that date: 13 positions, -$77.50 net, 4 stops for -$309. SOLANO (52064)
# lost $120.60 in 59 seconds and nothing recorded the curve's reserves, fee or tax at entry.

def test_the_round_trip_matches_the_hand_computed_curve_arithmetic():
    """Golden numbers. 1 ETH into a 100 ETH / 1e9 token curve at 100 bps fee and 50 bps tax."""
    amount, quote_reserve, token_reserve = 10**18, 100 * 10**18, 10**27

    pct, tokens = pons_round_trip(amount, quote_reserve, token_reserve, 100, 50)

    assert tokens == 9657326579856529056901623
    assert round(pct, 10) == -4.9082477500
    # two router fees, two curve fees, two taxes and the curve's own slippage, all of it paid twice
    assert -6.0 < pct < -4.0


def test_the_round_trip_is_the_compounded_fees_and_not_the_clip_size():
    """A constant-product round trip costs the FEES and nothing else, at any size.

    The price our buy moves against us is handed straight back when we sell into the reserves the
    buy just made, so slippage cancels exactly and only the fees survive. Measured across four
    clip sizes spanning six orders of magnitude, the answer is the same to ten decimal places and
    equals `(1-router)^2 * (1-fee-tax)^2 - 1`.

    THIS IS THE GATE'S REACH AND ALSO ITS LIMIT. It refuses an expensive CURVE and it can never
    refuse a large clip on a thin one, because that risk is not in this number at all. That is
    what `clip_share_pct` is recorded for.
    """
    qr, tr = 10**18, 10**18
    sizes = [pons_round_trip(a, qr, tr, 100, 50)[0]
             for a in (10**15, 10**17, 10**18, 10**21)]
    fee_only = 100.0 * ((0.99 * 0.985) ** 2 - 1.0)

    assert max(sizes) - min(sizes) < 1e-9
    assert abs(sizes[0] - fee_only) < 1e-4


def test_a_clip_far_larger_than_the_curve_still_returns_a_number():
    """Constant product never fully drains, so the drained-curve guard is defensive, not reachable."""
    pct, tokens = pons_round_trip(10**30, 10**18, 10**18, 100, 50)

    assert pct is not None and tokens < 10**18


def test_a_confiscatory_fee_is_refused_rather_than_priced():
    assert pons_round_trip(10**18, 100 * 10**18, 10**27, 9000, 1500) is None


def test_the_buy_route_carries_the_curve_state_and_its_cost():
    quote = dex_pons_v2_quote(CHAIN, WETH, MINT, 10**16, TAKER, call_fn=_call(),
                              slippage_bps=100, now_fn=lambda: 1_700_000_000)
    route = quote["route"]

    assert route["quote_reserve"] == str(2 * 10**18)
    assert route["token_reserve"] == str(10**27)
    assert route["sellable"] == str(10**30)
    assert route["graduated"] == 0
    assert route["round_trip_pct"] is not None and route["round_trip_pct"] < 0
    assert 0.0 < route["clip_share_pct"] < 100.0
    # the state a stop can be explained from later, which the ledger did not have for SOLANO
    assert route["curve_fee_bps"] == 100 and route["creator_tax_bps"] == 200


def test_the_sell_route_is_unchanged_and_carries_no_cost():
    """An exit is not a decision we can decline, so pricing its round trip would have no reader."""
    quote = dex_pons_v2_quote(CHAIN, MINT, WETH, 10**16, TAKER, call_fn=_call(),
                              slippage_bps=100, now_fn=lambda: 1_700_000_000)

    assert "round_trip_pct" not in quote["route"]
    assert "quote_reserve" not in quote["route"]


def _ladder(*, is_buy=True):
    """Run the real ladder with only the pons rung answering, and return the rung ledger."""
    from sniper.execution.evm_real_seams import venue_quote_with_v2_fallback
    call = _call()
    rungs = []

    def _pons(chain, sell, buy, amount, taker, **kw):
        return dex_pons_v2_quote(chain, sell, buy, amount, taker, call_fn=call,
                                 slippage_bps=100, now_fn=lambda: 1_700_000_000)
    quote_fn = venue_quote_with_v2_fallback(
        lambda s, b, a, slippage_bps=None: None,
        chain=CHAIN, quote_token=WETH, taker=TAKER,
        v2_quote_fn=lambda *a, **k: None, pons_v2_quote_fn=_pons,
        acct=object(), rpc_url="https://rpc", on_rung=rungs.append)
    sell_token, buy_token = (WETH, MINT) if is_buy else (MINT, WETH)
    out = quote_fn(sell_token, buy_token, 10**16, slippage_bps=100)
    return out, rungs


def test_a_pons_entry_over_the_budget_is_refused_and_names_its_cost(monkeypatch):
    from sniper.execution import evm_real_seams as S
    monkeypatch.setattr(S, "_entry_tp_pct", lambda _chain: 1.0)   # a 1% TP: nothing can pay for it

    out, rungs = _ladder(is_buy=True)

    assert out is None
    # the ladder unpacks a rung's 3-tuple as (result, blind_reason, REASON), so the cost lands in
    # `reason` -- the same field the multihop rung's refusal uses
    assert any("round_trip_over_budget" in str(r.get("reason") or "") for r in rungs), rungs


def test_a_pons_entry_within_the_budget_is_admitted(monkeypatch):
    from sniper.execution import evm_real_seams as S
    monkeypatch.setattr(S, "_entry_tp_pct", lambda _chain: 400.0)  # a TP the round trip fits under

    out, rungs = _ladder(is_buy=True)

    assert out is not None
    assert "round_trip_over_budget" not in str(rungs)


def test_a_pons_exit_is_never_refused_on_cost(monkeypatch):
    """The alternative to an expensive exit is a bag we cannot sell at all."""
    from sniper.execution import evm_real_seams as S
    monkeypatch.setattr(S, "_entry_tp_pct", lambda _chain: 1.0)

    out, rungs = _ladder(is_buy=False)

    assert out is not None
    assert "round_trip_over_budget" not in str(rungs)


def test_a_pons_quote_without_a_measurement_is_admitted_and_a_measured_none_is_refused(monkeypatch):
    """The cost gate acts only on a quote that measured its round trip. A quote with no
    `round_trip_pct` key (custom quote_fn, older shape) is admitted as before the gate existed;
    a measured None (curve unpriceable) fails closed."""
    from sniper.execution import evm_real_seams as seams
    from sniper.settings import memecoin as cfg
    from sniper.execution import geckoterminal_quote as gt
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_ENABLED", False)
    monkeypatch.setattr(gt, "cooldown_active", lambda *a, **k: False)
    WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
    MINT = "0xf0568863195770965a6d8abb0aa87f4314b80320"

    def ladder(pons):
        rungs = []
        out = seams.venue_quote_with_v2_fallback(
            lambda s, b, a, slippage_bps=None: None, chain="4663", quote_token=WETH,
            taker="0xtaker", v2_quote_fn=lambda *a, **k: None,
            pons_v2_quote_fn=lambda *a, **k: pons, acct=object(), rpc_url="https://rpc",
            on_rung=rungs.append)(WETH, MINT, 10 ** 18)
        return out, [r for r in rungs if r["rung"] == "pons_v2"]
    bare = {"buyAmount": "42"}
    out, _ = ladder(bare)
    assert out is bare
    measured_none = {"buyAmount": "42", "route": {"protocol": "pons_v2", "round_trip_pct": None}}
    out, rungs = ladder(measured_none)
    assert out is None and rungs and rungs[-1].get("reason", "").startswith("round_trip_over_budget")


# ── creator-tax test (2026-09-12, operator "b") ─────────────────────────────


def test_an_armed_test_entry_admits_its_own_over_budget_pons_buy(monkeypatch):
    from sniper.execution import evm_real_seams as S
    from sniper.execution import evm_pons_v2_quote as P
    monkeypatch.setattr(S, "_entry_tp_pct", lambda _chain: 10.0)   # budget 4%; the fixture costs ~7.8%
    monkeypatch.setattr(P, "TEST_ALLOW", {})
    assert _ladder(is_buy=True)[0] is None, "not armed: the normal budget refuses"
    P.TEST_ALLOW[(CHAIN, MINT)] = 14.0
    out, rungs = _ladder(is_buy=True)
    assert out is not None and "round_trip_over_budget" not in str(rungs)
    P.TEST_ALLOW[(CHAIN, MINT)] = 5.0
    assert _ladder(is_buy=True)[0] is None, "the test cap is still a cap"
    P.TEST_ALLOW.clear()
    P.TEST_ALLOW[(CHAIN, "0x" + "cd" * 20)] = 14.0
    assert _ladder(is_buy=True)[0] is None, "an allowance for another coin changes nothing"


def test_the_test_reads_the_curve_round_trip_and_nothing_for_a_non_pons_coin():
    from sniper.execution.evm_pons_v2_quote import test_round_trip_pct
    rt = test_round_trip_pct(CHAIN, MINT, call_fn=_call())
    assert rt is not None and -10.0 < rt < -5.0        # 1% router + 1% curve + 2% tax, each way
    assert test_round_trip_pct(CHAIN, MINT, call_fn=lambda to, data: None) is None
    assert test_round_trip_pct("56", MINT, call_fn=_call()) is None


def _arm_case(monkeypatch, *, wallets=("cryo",), rt=-13.4, max_trades=20, stored=()):
    import sqlite3, types
    from sniper.persistence import db_memecoin as m
    from sniper.services import memecoin_real_runner as R
    from sniper.execution import evm_real_seams as S
    from sniper.execution import evm_pons_v2_quote as P
    from sniper.settings import memecoin as cfg
    conn = sqlite3.connect(":memory:")
    m.ensure_memecoin_tables(conn)
    for i, stored_rt in enumerate(stored):
        conn.execute("INSERT INTO memecoin_positions (source_user_id, mint, execution_mode, network_id,"
                     " is_open, evm_route_json) VALUES ('cryo', ?, 'real', '4663', 0, ?)",
                     (f"0x{i:040x}", json.dumps({"protocol": "pons_v2", "round_trip_pct": stored_rt})))
    monkeypatch.setattr(cfg, "MEMECOIN_PONS_TEST_WALLETS", frozenset(wallets))
    monkeypatch.setattr(cfg, "MEMECOIN_PONS_TEST_MAX_TRADES", max_trades)
    monkeypatch.setattr(cfg, "MEMECOIN_PONS_TEST_MAX_ROUND_TRIP_PCT", 14.0)
    monkeypatch.setattr(S, "_entry_tp_pct", lambda _chain: 25.0)
    monkeypatch.setattr(P, "test_round_trip_pct", lambda chain, mint, **k: rt)
    monkeypatch.setattr(P, "TEST_ALLOW", {})
    ev = types.SimpleNamespace(source_user_id="CRYO", mint=MINT.upper().replace("0X", "0x"))
    return R._pons_test_arm(conn, ev, "4663"), P.TEST_ALLOW


def test_the_arm_fires_only_for_a_listed_wallet_on_an_over_budget_curve(monkeypatch):
    key, allow = _arm_case(monkeypatch)
    assert key == ("4663", MINT) and allow[key] == 14.0
    assert _arm_case(monkeypatch, wallets=("someone",))[0] is None
    assert _arm_case(monkeypatch, rt=-9.0)[0] is None, "within the normal budget: nothing to test"
    assert _arm_case(monkeypatch, rt=-15.0)[0] is None, "over the test cap"
    assert _arm_case(monkeypatch, rt=None)[0] is None, "not a Pons curve, or unreadable"


def test_the_arm_stops_at_the_trade_cap_and_counts_only_test_positions(monkeypatch):
    assert _arm_case(monkeypatch, max_trades=2, stored=(-13.4, -13.4))[0] is None
    # normal-budget Pons positions are not test positions
    assert _arm_case(monkeypatch, max_trades=2, stored=(-13.4, -7.8, -9.6))[0] is not None


def test_the_hook_sizes_down_and_disarms_after_the_entry():
    import inspect
    from sniper.services.memecoin_real_runner import real_entry_hook
    src = inspect.getsource(real_entry_hook)
    # The cap must reach the reservation, not only trade_size (Codex P1, 2026-09-13). The
    # behavior is proven in test_memecoin_evm_leg::test_a_hard_size_cap_bounds_the_reservation...
    assert "_size_cap = float(cfg.MEMECOIN_PONS_TEST_CLIP_USD)" in src
    assert "size_cap_usd=_size_cap" in src[src.index("res = real_entry("):]
    i = src.index("_pons_key = _pons_test_arm(")
    assert src.index("finally:", i) < src.index("TEST_ALLOW.pop(_pons_key", i)
    assert src.index("_pons_key = _pons_test_arm(") < src.index("res = real_entry(")


def test_the_real_caller_reads_the_seven_getters_in_parallel():
    """Every graduated Pons coin paid 1 + 7 SERIAL reads before the rung answered None, and the
    SELL ladder waits for this rung before it may take v4 (ROOM 64049, 2026-09-23: 15.9 s in two
    quotes). The seven getters go out together, in order, one round trip."""
    import time
    from sniper.execution.evm_pons_v2_quote import _make_call_fn

    def http(_method, _url, body=None, **_kw):
        time.sleep(0.2)
        return 200, {"jsonrpc": "2.0", "id": 1, "result": _word(7)}, None

    call = _make_call_fn("https://rpc.test.invalid", http=http)
    calls = [("0x" + "ab" * 20, "0x%08x" % i) for i in range(7)]
    started = time.perf_counter()
    values = call.batch(calls)
    elapsed = time.perf_counter() - started
    assert values == [_word(7)] * 7
    assert elapsed < 0.9, "seven 0.2 s reads took %.2fs: they ran one after another" % elapsed
