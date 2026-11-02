"""TDD for sniper/execution/evm_launchpad_curve.py.

The `cpamm_out` golden numbers below are not invented: they are the curve's own live
`getReserves()`/`feeBps()` values on chain 4663, paired with real `eth_simulateV1` buy and sell
results, gathered while building the design spec
(docs/superpowers/specs/2026-09-17-memecoin-4663-launchpad-curve-venue-design.md). All 6 checks
(3 buy sizes, 3 sell sizes) agreed with the simulated chain within 0.1%; the buy checks agreed to
the last few atomic units.
"""
import json

import pytest

from sniper.execution import evm_launchpad_curve as C

CURVE = "0xd1255489d894452dd234a001f0fbcab6d4037040"
COIN = "0x5e9ca04c6fe6397f287091130a63072a91afd14f"
COUNTER_NATIVE = "0x" + "00" * 20
TAKER = "0x898be9c27f8b391c47a3d9f2da17781aa8aa3a1d"
LAUNCHPAD = "0x7ed598bcef8bd9edd8c97a195c6d13f40801ec7e"


# -- cpamm_out: golden numbers from a real curve, real eth_simulateV1 buys and sells ---------

def test_cpamm_out_matches_real_buy_small():
    out = C.cpamm_out(1689900000000000001, 994141665187289188709391089, 2049692751056616, 100)
    assert out == 1192312373349192360969699


def test_cpamm_out_matches_real_buy_medium():
    out = C.cpamm_out(1689900000000000001, 994141665187289188709391089, 20496927510566168, 100)
    assert out == 11795799327068399941660600


def test_cpamm_out_matches_real_buy_large():
    out = C.cpamm_out(1689900000000000001, 994141665187289188709391089, 122981565063396992, 100)
    assert out == 66811122468710529479888113


def test_cpamm_out_matches_real_sell():
    # reserves read from getReserves() immediately after the paired real buy, in the same
    # eth_simulateV1 bundle -- see the spec's phase-3 evidence for the exact chained call.
    out = C.cpamm_out(982345865860220788377133875, 1710191958235460508,
                      11795799327068400332257214, 100)
    real_payout = 20089038653105901
    assert abs(out - real_payout) / real_payout < 0.001          # within 0.1%, per the spec's bar


def test_cpamm_out_zero_amount_is_zero():
    assert C.cpamm_out(1000, 1000, 0, 100) == 0


def test_cpamm_out_empty_reserve_is_zero():
    assert C.cpamm_out(0, 1000, 500, 100) == 0
    assert C.cpamm_out(1000, 0, 500, 100) == 0


# -- calldata encoders: exact byte layout, matching the confirmed selectors -------------------

def test_encode_buy_layout():
    data = C.encode_buy(1722062479840703, 970000000000000000000000,
                        "0x9e2c20aaa4ecb61c41dc090d1f28bf24b1c38807")
    assert data.startswith("0x59a87bc1")
    assert len(data) == 2 + 8 + 64 * 3
    body = data[10:]
    assert int(body[0:64], 16) == 1722062479840703
    assert int(body[64:128], 16) == 970000000000000000000000
    assert body[128:] == "0000000000000000000000009e2c20aaa4ecb61c41dc090d1f28bf24b1c38807"


def test_encode_sell_layout():
    data = C.encode_sell(35997610571813712674743314, 56488767237985744,
                         "0x257de775eeeb24a8b8055aa3d11e03434a34d80a")
    assert data.startswith("0xd04c6983")
    body = data[10:]
    assert int(body[0:64], 16) == 35997610571813712674743314
    assert int(body[64:128], 16) == 56488767237985744
    assert body[128:] == "000000000000000000000000257de775eeeb24a8b8055aa3d11e03434a34d80a"


def test_encode_approve_is_exact_amount_never_max_uint():
    data = C.encode_approve(CURVE, 60_000_000)
    body = data[10:]
    assert int(body[64:128], 16) == 60_000_000
    assert int(body[64:128], 16) != 2 ** 256 - 1


# -- curve_state / curve_reserves: parse a real info()/getReserves() shape --------------------

def _pad(word_int_or_addr):
    if isinstance(word_int_or_addr, str):
        return word_int_or_addr[2:].lower().rjust(64, "0")
    return f"{word_int_or_addr:064x}"


def _info_words(coin=COIN, curve=CURVE, counter=COUNTER_NATIVE, state=0):
    words = [_pad(0)] * 15
    words[0] = _pad(coin)
    words[1] = _pad(curve)
    words[4] = _pad(counter)
    words[10] = _pad(state)
    return "0x" + "".join(words)


def _http_for(info_hex, fee_bps=100, reserves=None):
    def http(method, url, headers=None, body=None, timeout=10):
        req = json.loads(body)
        meth, params = req["method"], req.get("params") or []
        if meth != "eth_call":
            return 200, {"result": "0x"}, None
        call = params[0]
        data = call.get("data") or ""
        to = (call.get("to") or "").lower()
        if to == LAUNCHPAD.lower():
            return 200, {"result": info_hex}, None
        if data.startswith("0x24a9d853"):
            return 200, {"result": "0x" + f"{fee_bps:064x}"}, None
        if data.startswith("0x0902f1ac") and reserves is not None:
            reserve_counter, reserve_coin = reserves
            return 200, {"result": "0x" + f"{reserve_counter:064x}" + f"{reserve_coin:064x}"}, None
        return 200, {"result": "0x"}, None
    return http


def test_curve_state_reads_coin_curve_counter_fee_state(monkeypatch):
    import sniper.settings.memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_LAUNCHPADS",
                        {"4663": ({"address": LAUNCHPAD, "info_selector": "3cf28b5a",
                                    "hook_selector": "6651812c", "coin_word": 0,
                                    "counter_word": 4, "fee_word": 6,
                                    "tick_spacing_word": 7},)}, raising=False)
    http = _http_for(_info_words(state=0))
    state = C.curve_state("4663", COIN, "https://rpc.example", http=http)
    assert state == {"coin": COIN.lower(), "curve": CURVE.lower(),
                     "counter": COUNTER_NATIVE, "fee_bps": 100, "state": 0}


def test_curve_state_none_when_launchpad_names_a_different_coin(monkeypatch):
    import sniper.settings.memecoin as cfg
    other_coin = "0x" + "11" * 20
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_LAUNCHPADS",
                        {"4663": ({"address": LAUNCHPAD, "info_selector": "3cf28b5a",
                                    "hook_selector": "6651812c", "coin_word": 0,
                                    "counter_word": 4, "fee_word": 6,
                                    "tick_spacing_word": 7},)}, raising=False)
    http = _http_for(_info_words(coin=other_coin))
    assert C.curve_state("4663", COIN, "https://rpc.example", http=http) is None


def test_curve_reserves_reads_counter_then_coin():
    http = _http_for(_info_words(), reserves=(1689900000000000001, 994141665187289188709391089))
    reserves = C.curve_reserves(CURVE, "https://rpc.example", http=http)
    assert reserves == (1689900000000000001, 994141665187289188709391089)


# -- dex_launchpad_curve_quote / _exit_quote: the rung-shaped entry points ---------------------

def _wired(state=None, reserves=(1689900000000000001, 994141665187289188709391089)):
    def state_fn(chain, mint, rpc_url, http=None):
        return state
    def reserves_fn(curve, rpc_url, http=None):
        return reserves
    return state_fn, reserves_fn


def test_quote_none_when_coin_already_graduated():
    state_fn, reserves_fn = _wired(
        state={"coin": COIN, "curve": CURVE, "counter": COUNTER_NATIVE,
               "fee_bps": 100, "state": 2})                      # graduated
    out = C.dex_launchpad_curve_quote(
        "4663", COUNTER_NATIVE, COIN, 10 ** 16, TAKER,
        rpc_url="https://rpc.example", state_fn=state_fn, reserves_fn=reserves_fn)
    assert out is None


def test_exit_quote_none_when_coin_already_graduated():
    """Pins condition 5: a curve sell must never be built once word 10 is not 0."""
    state_fn, reserves_fn = _wired(
        state={"coin": COIN, "curve": CURVE, "counter": COUNTER_NATIVE,
               "fee_bps": 100, "state": 2})
    out = C.dex_launchpad_curve_exit_quote(
        "4663", COIN, COUNTER_NATIVE, 10 ** 18, TAKER,
        rpc_url="https://rpc.example", state_fn=state_fn, reserves_fn=reserves_fn)
    assert out is None


def test_quote_none_when_sell_token_is_not_the_curves_counter():
    state_fn, reserves_fn = _wired(
        state={"coin": COIN, "curve": CURVE, "counter": "0x" + "22" * 20,
               "fee_bps": 100, "state": 0})
    out = C.dex_launchpad_curve_quote(
        "4663", COUNTER_NATIVE, COIN, 10 ** 16, TAKER,
        rpc_url="https://rpc.example", state_fn=state_fn, reserves_fn=reserves_fn)
    assert out is None


def test_quote_min_amount_out_is_never_zero_for_a_real_trade():
    """Pins condition 1: minAmountOut is never 0 on a real quote's built transaction."""
    state_fn, reserves_fn = _wired(
        state={"coin": COIN, "curve": CURVE, "counter": COUNTER_NATIVE,
               "fee_bps": 100, "state": 0})
    out = C.dex_launchpad_curve_quote(
        "4663", COUNTER_NATIVE, COIN, 10 ** 16, TAKER,
        rpc_url="https://rpc.example", slippage_bps=300,
        state_fn=state_fn, reserves_fn=reserves_fn)
    assert out is not None
    assert int(out["minBuyAmount"]) > 0
    encoded_min_out = int(out["transaction"]["data"][10:][64:128], 16)
    assert encoded_min_out > 0
    assert encoded_min_out == int(out["minBuyAmount"])


def test_quote_builds_native_value_transaction():
    state_fn, reserves_fn = _wired(
        state={"coin": COIN, "curve": CURVE, "counter": COUNTER_NATIVE,
               "fee_bps": 100, "state": 0})
    out = C.dex_launchpad_curve_quote(
        "4663", COUNTER_NATIVE, COIN, 10 ** 16, TAKER,
        rpc_url="https://rpc.example", state_fn=state_fn, reserves_fn=reserves_fn)
    assert out["transaction"]["to"] == CURVE.lower()
    assert int(out["transaction"]["value"], 16) == 10 ** 16
    assert out["issues"] == {}                                   # no approval needed for native
    assert out["pre_transactions"] == []                         # already native, no unwrap needed


def test_quote_accepts_the_chains_weth_as_the_native_counter_and_unwraps_first():
    """The real ladder calls this rung with sell_token=WETH (CHAIN_QUOTE), never the literal
    native address -- our wallet holds WETH, not native, on chain 4663. A native-counter curve
    quote must accept WETH as the cash side and unwrap it via `pre_transactions` before the curve
    buy, matching how dex_v4_quote's native buy path already does this."""
    from sniper.execution.evm_wallet import CHAIN_QUOTE
    weth = CHAIN_QUOTE["4663"]["token"].lower()
    state_fn, reserves_fn = _wired(
        state={"coin": COIN, "curve": CURVE, "counter": COUNTER_NATIVE,
               "fee_bps": 100, "state": 0})
    out = C.dex_launchpad_curve_quote(
        "4663", weth, COIN, 10 ** 16, TAKER,
        rpc_url="https://rpc.example", state_fn=state_fn, reserves_fn=reserves_fn)
    assert out is not None
    assert int(out["transaction"]["value"], 16) == 10 ** 16       # curve still paid in native
    assert len(out["pre_transactions"]) == 1
    pre = out["pre_transactions"][0]
    assert pre["to"] == weth
    assert pre["data"].startswith("0x2e1a7d4d")                   # WETH.withdraw(uint256)
    assert int(pre["data"][10:], 16) == 10 ** 16
    assert pre["value"] == "0x0"
    from sniper.services.memecoin_evm_gas_topup import WETH_CALL_GAS_LIMIT
    assert pre["gas"] == str(WETH_CALL_GAS_LIMIT)                  # one limit for WETH calls


def test_quote_none_when_paying_token_is_neither_native_nor_weth():
    state_fn, reserves_fn = _wired(
        state={"coin": COIN, "curve": CURVE, "counter": COUNTER_NATIVE,
               "fee_bps": 100, "state": 0})
    out = C.dex_launchpad_curve_quote(
        "4663", "0x" + "44" * 20, COIN, 10 ** 16, TAKER,
        rpc_url="https://rpc.example", state_fn=state_fn, reserves_fn=reserves_fn)
    assert out is None


def test_exit_quote_accepts_weth_as_buy_token_for_a_native_counter_and_needs_no_unwrap():
    """The exit side receives native directly from the curve -- no pre_transactions, no wrap
    step. Realized proceeds are picked up by the existing native-receipt fallback in
    evm_real_seams.py (see the comment in dex_launchpad_curve_exit_quote)."""
    from sniper.execution.evm_wallet import CHAIN_QUOTE
    weth = CHAIN_QUOTE["4663"]["token"].lower()
    state_fn, reserves_fn = _wired(
        state={"coin": COIN, "curve": CURVE, "counter": COUNTER_NATIVE,
               "fee_bps": 100, "state": 0})
    out = C.dex_launchpad_curve_exit_quote(
        "4663", COIN, weth, 10 ** 18, TAKER,
        rpc_url="https://rpc.example", state_fn=state_fn, reserves_fn=reserves_fn)
    assert out is not None
    assert out["transaction"]["value"] == "0x0"
    assert "pre_transactions" not in out                          # no wrap/unwrap step needed


def test_quote_builds_erc20_allowance_issue_not_native_value():
    stable = "0x" + "33" * 20
    state_fn, reserves_fn = _wired(
        state={"coin": COIN, "curve": CURVE, "counter": stable, "fee_bps": 100, "state": 0})
    out = C.dex_launchpad_curve_quote(
        "4663", stable, COIN, 60_000_000, TAKER,
        rpc_url="https://rpc.example", state_fn=state_fn, reserves_fn=reserves_fn)
    assert out["transaction"]["value"] == "0x0"
    assert out["issues"] == {"allowance": {"spender": CURVE.lower()}}


def test_exit_quote_min_amount_out_is_never_zero():
    state_fn, reserves_fn = _wired(
        state={"coin": COIN, "curve": CURVE, "counter": COUNTER_NATIVE,
               "fee_bps": 100, "state": 0})
    out = C.dex_launchpad_curve_exit_quote(
        "4663", COIN, COUNTER_NATIVE, 10 ** 22, TAKER,
        rpc_url="https://rpc.example", slippage_bps=300,
        state_fn=state_fn, reserves_fn=reserves_fn)
    assert out is not None
    assert int(out["minBuyAmount"]) > 0
    encoded_min_out = int(out["transaction"]["data"][10:][64:128], 16)
    assert encoded_min_out > 0


# -- whole_balance_exit_quote_or_refuse: this rung's own clip-size check -----------------------

def test_clip_refuse_when_state_unreadable():
    state_fn = lambda chain, mint, rpc_url, http=None: None
    quote, reason = C.whole_balance_exit_quote_or_refuse(
        "4663", COIN, 10 ** 18, "https://rpc.example", taker=TAKER, state_fn=state_fn)
    assert quote is None
    assert reason == C.STATE_UNREADABLE


def test_clip_refuse_when_graduated():
    state_fn = lambda chain, mint, rpc_url, http=None: {
        "coin": COIN, "curve": CURVE, "counter": COUNTER_NATIVE, "fee_bps": 100, "state": 2}
    quote, reason = C.whole_balance_exit_quote_or_refuse(
        "4663", COIN, 10 ** 18, "https://rpc.example", taker=TAKER, state_fn=state_fn)
    assert quote is None
    assert reason == C.GRADUATED_NO_CONFIRMED_KEY


def test_clip_refuse_when_curve_cannot_price_the_whole_balance():
    state_fn = lambda chain, mint, rpc_url, http=None: {
        "coin": COIN, "curve": CURVE, "counter": COUNTER_NATIVE, "fee_bps": 100, "state": 0}
    reserves_fn = lambda curve, rpc_url, http=None: None          # curve unreadable -> no quote
    quote, reason = C.whole_balance_exit_quote_or_refuse(
        "4663", COIN, 10 ** 18, "https://rpc.example", taker=TAKER,
        state_fn=state_fn, reserves_fn=reserves_fn)
    assert quote is None
    assert reason == C.EXIT_UNQUOTABLE


def test_clip_check_succeeds_on_a_real_quotable_curve():
    state_fn, reserves_fn = _wired(
        state={"coin": COIN, "curve": CURVE, "counter": COUNTER_NATIVE,
               "fee_bps": 100, "state": 0})
    quote, reason = C.whole_balance_exit_quote_or_refuse(
        "4663", COIN, 10 ** 18, "https://rpc.example", taker=TAKER,
        state_fn=state_fn, reserves_fn=reserves_fn)
    assert reason is None
    assert quote is not None
    assert int(quote["minBuyAmount"]) > 0


# -- exit_quote: the graduation switch -----------------------------------------------------------

def test_exit_quote_uses_the_curve_while_state_is_zero():
    state_fn, reserves_fn = _wired(
        state={"coin": COIN, "curve": CURVE, "counter": COUNTER_NATIVE,
               "fee_bps": 100, "state": 0})
    quote, reason = C.exit_quote(
        "4663", COIN, 10 ** 18, "https://rpc.example", taker=TAKER,
        state_fn=state_fn, reserves_fn=reserves_fn)
    assert reason is None
    assert quote["route"]["protocol"] == "launchpad_curve"


def test_exit_quote_never_builds_a_curve_sell_once_graduated():
    """Pins condition 5 end to end: state=2 must never reach encode_sell/the curve."""
    state_fn = lambda chain, mint, rpc_url, http=None: {
        "coin": COIN, "curve": CURVE, "counter": COUNTER_NATIVE, "fee_bps": 100, "state": 2}
    calls = {"v4": 0}

    def derived_pool_keys_fn(chain, mint, rpc_url, http=None):
        return [("0xpoolid", {"currency0": COUNTER_NATIVE, "currency1": COIN,
                              "fee": 0, "tick_spacing": 200, "hooks": "0xhook"}, True)]

    def v4_quote_fn(chain, mint, amount_atomic, taker, confirmed, *, http=None,
                    slippage_bps=None, rpc_url=None):
        calls["v4"] += 1
        return {"route": "derived_v4", "confirmed": confirmed}

    quote, reason = C.exit_quote(
        "4663", COIN, 10 ** 18, "https://rpc.example", taker=TAKER, state_fn=state_fn,
        derived_pool_keys_fn=derived_pool_keys_fn, v4_quote_fn=v4_quote_fn)
    assert reason is None
    assert quote["route"] == "derived_v4"
    assert calls["v4"] == 1                       # the V4 path was used, not the curve


def test_exit_quote_refuses_graduated_with_no_confirmed_v4_key():
    state_fn = lambda chain, mint, rpc_url, http=None: {
        "coin": COIN, "curve": CURVE, "counter": COUNTER_NATIVE, "fee_bps": 100, "state": 2}
    derived_pool_keys_fn = lambda chain, mint, rpc_url, http=None: []
    quote, reason = C.exit_quote(
        "4663", COIN, 10 ** 18, "https://rpc.example", taker=TAKER, state_fn=state_fn,
        derived_pool_keys_fn=derived_pool_keys_fn)
    assert quote is None
    assert reason == C.GRADUATED_NO_CONFIRMED_KEY


def test_exit_quote_switches_mid_hold_across_two_calls():
    """One position, still on its curve at first read, graduated by the second -- the caller must
    get the curve quote first, then the derived-V4 quote next, with no code change in between."""
    state = {"coin": COIN, "curve": CURVE, "counter": COUNTER_NATIVE, "fee_bps": 100, "state": 0}
    state_fn = lambda chain, mint, rpc_url, http=None: dict(state)
    reserves_fn = lambda curve, rpc_url, http=None: (1689900000000000001,
                                                      994141665187289188709391089)

    quote1, reason1 = C.exit_quote("4663", COIN, 10 ** 18, "https://rpc.example", taker=TAKER,
                                   state_fn=state_fn, reserves_fn=reserves_fn)
    assert reason1 is None
    assert quote1["route"]["protocol"] == "launchpad_curve"

    state["state"] = 2                                          # coin graduates mid-hold
    quote2, reason2 = C.exit_quote(
        "4663", COIN, 10 ** 18, "https://rpc.example", taker=TAKER, state_fn=state_fn,
        derived_pool_keys_fn=lambda *a, **k: [("0xpid", {}, True)],
        v4_quote_fn=lambda *a, **k: {"route": "derived_v4"})
    assert reason2 is None
    assert quote2["route"] == "derived_v4"


# -- mark_price_entry: the getReserves()-derived mid mark --------------------------------------

def test_mark_price_entry_none_when_graduated():
    state_fn = lambda chain, mint, rpc_url, http=None: {
        "coin": COIN, "curve": CURVE, "counter": COUNTER_NATIVE, "fee_bps": 100, "state": 2}
    assert C.mark_price_entry("4663", COIN, "https://rpc.example", state_fn=state_fn) is None


def test_mark_price_entry_writes_source_launchpad_curve_quality_live(monkeypatch):
    monkeypatch.setattr("sniper.execution.evm_wallet.erc20_decimals", lambda *a, **k: 18)
    monkeypatch.setattr("sniper.execution.evm_wallet.native_usd_price", lambda *a, **k: 2000.0)
    state_fn, reserves_fn = _wired(
        state={"coin": COIN, "curve": CURVE, "counter": COUNTER_NATIVE,
               "fee_bps": 100, "state": 0})
    entry = C.mark_price_entry("4663", COIN, "https://rpc.example",
                               state_fn=state_fn, reserves_fn=reserves_fn, observed_at="now")
    assert entry["source"] == "launchpad_curve"
    assert entry["quality"] == "live"
    assert entry["price"] == pytest.approx(
        1689900000000000001 / 994141665187289188709391089 * 2000)
    assert entry["observed_at"] == "now"


# -- wrap_token_safety_fn: this rung's whole-balance check ahead of the standard cascade --------

def _fallback_recording():
    calls = []

    def fallback(mint, *, deadline=None, usd=None, bought_amount_atomic=None, **kwargs):
        calls.append((mint, deadline, usd, bought_amount_atomic))
        return True, "standard_probe_ok"
    return fallback, calls


def test_safety_wrapper_falls_through_when_usd_is_not_supplied():
    fallback, calls = _fallback_recording()
    wrapped = C.wrap_token_safety_fn("4663", "https://rpc.example", taker=TAKER, fallback_fn=fallback)
    assert wrapped(COIN) == (True, "standard_probe_ok")
    assert calls == [(COIN, None, None, None)]


def test_safety_wrapper_falls_through_for_a_non_launchpad_mint():
    fallback, calls = _fallback_recording()
    state_fn = lambda chain, mint, rpc_url, http=None: None
    wrapped = C.wrap_token_safety_fn(
        "4663", "https://rpc.example", taker=TAKER, fallback_fn=fallback, state_fn=state_fn)
    assert wrapped(COIN, usd=50.0) == (True, "standard_probe_ok")
    assert calls == [(COIN, None, 50.0, None)]


def test_safety_wrapper_falls_through_once_graduated():
    fallback, calls = _fallback_recording()
    state_fn = lambda chain, mint, rpc_url, http=None: {
        "coin": COIN, "curve": CURVE, "counter": COUNTER_NATIVE, "fee_bps": 100, "state": 2}
    wrapped = C.wrap_token_safety_fn(
        "4663", "https://rpc.example", taker=TAKER, fallback_fn=fallback, state_fn=state_fn)
    assert wrapped(COIN, usd=50.0) == (True, "standard_probe_ok")
    assert calls == [(COIN, None, 50.0, None)]


def test_safety_wrapper_checks_the_curve_directly_for_a_live_native_counter_mint():
    fallback, calls = _fallback_recording()
    state_fn, reserves_fn = _wired(
        state={"coin": COIN, "curve": CURVE, "counter": COUNTER_NATIVE,
               "fee_bps": 100, "state": 0})
    wrapped = C.wrap_token_safety_fn(
        "4663", "https://rpc.example", taker=TAKER, fallback_fn=fallback,
        state_fn=state_fn, reserves_fn=reserves_fn, native_usd_price_fn=lambda chain: 2000.0)
    ok, reason = wrapped(COIN, usd=50.0)
    assert ok is True
    assert reason == "launchpad_curve_exit_quotable_at_clip"
    assert calls == []                                              # never touched the standard cascade


def test_safety_wrapper_refuses_when_the_curve_cannot_price_the_expected_position():
    fallback, calls = _fallback_recording()
    state_fn = lambda chain, mint, rpc_url, http=None: {
        "coin": COIN, "curve": CURVE, "counter": COUNTER_NATIVE, "fee_bps": 100, "state": 0}
    reserves_fn = lambda curve, rpc_url, http=None: None            # curve unreadable
    wrapped = C.wrap_token_safety_fn(
        "4663", "https://rpc.example", taker=TAKER, fallback_fn=fallback,
        state_fn=state_fn, reserves_fn=reserves_fn, native_usd_price_fn=lambda chain: 2000.0)
    ok, reason = wrapped(COIN, usd=50.0)
    assert ok is False
    assert calls == []


def test_safety_wrapper_accepts_weth_the_same_as_native():
    from sniper.execution.evm_wallet import CHAIN_QUOTE
    weth = CHAIN_QUOTE["4663"]["token"].lower()
    fallback, calls = _fallback_recording()
    state_fn, reserves_fn = _wired(
        state={"coin": COIN, "curve": CURVE, "counter": COUNTER_NATIVE,
               "fee_bps": 100, "state": 0})
    wrapped = C.wrap_token_safety_fn(
        "4663", "https://rpc.example", taker=TAKER, fallback_fn=fallback,
        state_fn=state_fn, reserves_fn=reserves_fn, native_usd_price_fn=lambda chain: 2000.0,
        weth=weth)
    ok, reason = wrapped(COIN, usd=50.0)
    assert ok is True


# -- bound identity (2026-09-17, matching 54ffd4ec): bought_amount_atomic, when supplied, is the
# amount checked -- never a second, independently-derived estimate --------------------------------

def test_safety_wrapper_signature_names_bought_amount_atomic_explicitly():
    """`_executor_supports_kwarg` name-matches this, not just VAR_KEYWORD -- pins the explicit
    parameter, not an incidental **kwargs catch."""
    import inspect
    fallback, _ = _fallback_recording()
    wrapped = C.wrap_token_safety_fn("4663", "https://rpc.example", taker=TAKER, fallback_fn=fallback)
    assert "bought_amount_atomic" in inspect.signature(wrapped).parameters


def test_safety_wrapper_uses_bought_amount_atomic_instead_of_deriving_its_own():
    """A price feed that would refuse (0.0) must not be consulted at all when the real bought
    amount is already known -- proves the estimate path is skipped, not merely correct by luck."""
    fallback, calls = _fallback_recording()
    state_fn, reserves_fn = _wired(
        state={"coin": COIN, "curve": CURVE, "counter": COUNTER_NATIVE,
               "fee_bps": 100, "state": 0})

    def price_fn_that_must_not_be_called(chain):
        raise AssertionError("bound identity must skip the usd-based entry-quote derivation")

    wrapped = C.wrap_token_safety_fn(
        "4663", "https://rpc.example", taker=TAKER, fallback_fn=fallback,
        state_fn=state_fn, reserves_fn=reserves_fn,
        native_usd_price_fn=price_fn_that_must_not_be_called)
    ok, reason = wrapped(COIN, usd=50.0, bought_amount_atomic=10 ** 20)
    assert ok is True
    assert reason == "launchpad_curve_exit_quotable_at_clip"
    assert calls == []


def test_safety_wrapper_refuses_the_bound_amount_when_the_curve_cannot_exit_it():
    fallback, calls = _fallback_recording()
    state_fn = lambda chain, mint, rpc_url, http=None: {
        "coin": COIN, "curve": CURVE, "counter": COUNTER_NATIVE, "fee_bps": 100, "state": 0}
    reserves_fn = lambda curve, rpc_url, http=None: None            # curve unreadable
    wrapped = C.wrap_token_safety_fn(
        "4663", "https://rpc.example", taker=TAKER, fallback_fn=fallback,
        state_fn=state_fn, reserves_fn=reserves_fn)
    ok, reason = wrapped(COIN, bought_amount_atomic=10 ** 20)
    assert ok is False
    assert calls == []


def test_safety_wrapper_forwards_bought_amount_atomic_on_pass_through():
    """Every mint the curve check does not own must still hand `bought_amount_atomic` on to the
    real (post-54ffd4ec) token-safety function unchanged -- never silently dropped."""
    fallback, calls = _fallback_recording()
    state_fn = lambda chain, mint, rpc_url, http=None: None          # not this launchpad
    wrapped = C.wrap_token_safety_fn(
        "4663", "https://rpc.example", taker=TAKER, fallback_fn=fallback, state_fn=state_fn)
    wrapped(COIN, usd=50.0, bought_amount_atomic=123456)
    assert calls == [(COIN, None, 50.0, 123456)]


def test_safety_wrapper_treats_a_zero_bound_amount_as_unknown_and_falls_back_to_usd():
    fallback, calls = _fallback_recording()
    state_fn, reserves_fn = _wired(
        state={"coin": COIN, "curve": CURVE, "counter": COUNTER_NATIVE,
               "fee_bps": 100, "state": 0})
    wrapped = C.wrap_token_safety_fn(
        "4663", "https://rpc.example", taker=TAKER, fallback_fn=fallback,
        state_fn=state_fn, reserves_fn=reserves_fn, native_usd_price_fn=lambda chain: 2000.0)
    ok, reason = wrapped(COIN, usd=50.0, bought_amount_atomic=0)
    assert ok is True
    assert reason == "launchpad_curve_exit_quotable_at_clip"
    assert calls == []


@pytest.mark.parametrize("fee_bps", [-1, 10000, 20000])
def test_invalid_curve_fee_cannot_produce_output(fee_bps):
    assert C.cpamm_out(1000, 1000, 2000, fee_bps) == 0


@pytest.mark.parametrize("sell", [False, True])
@pytest.mark.parametrize("slippage_bps", [-1, 10000, 10001, 9999])
def test_curve_quote_requires_a_positive_valid_minimum(sell, slippage_bps):
    state_fn, _ = _wired(state={"coin": COIN, "curve": CURVE,
                               "counter": COUNTER_NATIVE, "fee_bps": 100, "state": 0})
    # The valid 9999 bps case rounds a one-unit output down to zero.
    reserves_fn = lambda *a, **k: (1000, 1000)
    quote_fn = C.dex_launchpad_curve_exit_quote if sell else C.dex_launchpad_curve_quote
    args = (COIN, COUNTER_NATIVE) if sell else (COUNTER_NATIVE, COIN)
    assert quote_fn("4663", *args, 2, TAKER, rpc_url="https://rpc.example",
                    slippage_bps=slippage_bps, state_fn=state_fn,
                    reserves_fn=reserves_fn) is None


@pytest.mark.parametrize("decimals", [6, 18])
def test_curve_mark_converts_atomic_reserves_to_usd(monkeypatch, decimals):
    monkeypatch.setattr("sniper.execution.evm_wallet.erc20_decimals", lambda *a, **k: decimals)
    monkeypatch.setattr("sniper.execution.evm_wallet.native_usd_price", lambda *a, **k: 2000.0)
    state_fn, _ = _wired(state={"coin": COIN, "curve": CURVE,
                               "counter": COUNTER_NATIVE, "fee_bps": 100, "state": 0})
    reserves_fn = lambda *a, **k: (2 * 10**18, 1000 * 10**decimals)
    entry = C.mark_price_entry("4663", COIN, "rpc", state_fn=state_fn, reserves_fn=reserves_fn)
    assert entry["price"] == pytest.approx(4.0)


@pytest.mark.parametrize("counter,decimals,usd", [
    ("0x" + "11" * 20, 18, 2000),
    (COUNTER_NATIVE, None, 2000),
    (COUNTER_NATIVE, 18, None),
    (COUNTER_NATIVE, 18, float("nan")),
])
def test_curve_mark_without_valid_units_stays_unknown(monkeypatch, counter, decimals, usd):
    monkeypatch.setattr("sniper.execution.evm_wallet.erc20_decimals", lambda *a, **k: decimals)
    monkeypatch.setattr("sniper.execution.evm_wallet.native_usd_price", lambda *a, **k: usd)
    state_fn, reserves_fn = _wired(state={"coin": COIN, "curve": CURVE,
                                        "counter": counter, "fee_bps": 100, "state": 0})
    assert C.mark_price_entry("4663", COIN, "rpc", state_fn=state_fn,
                             reserves_fn=reserves_fn) is None


@pytest.mark.parametrize("expired", [False, True])
def test_curve_safety_uses_the_shared_rpc_deadline(expired):
    from contextlib import contextmanager
    active = []

    @contextmanager
    def scope(deadline):
        active.append(deadline)
        try:
            yield lambda: expired
        finally:
            active.pop()

    def state_fn(*args, **kwargs):
        assert active == [123.0]
        return {"coin": COIN, "curve": CURVE, "counter": COUNTER_NATIVE,
                "fee_bps": 100, "state": 0}

    wrapped = C.wrap_token_safety_fn(
        "4663", "rpc", taker=TAKER, fallback_fn=lambda *a, **k: (False, "fallback"),
        state_fn=state_fn, reserves_fn=lambda *a, **k: (100000, 100000),
        safety_deadline_scope=scope)
    safe, reason = wrapped(COIN, deadline=123.0, bought_amount_atomic=100)
    assert safe is (not expired)
    assert reason == ("safety_deadline_exceeded" if expired else "launchpad_curve_exit_quotable_at_clip")
    assert not active


def test_curve_safety_price_read_uses_the_bounded_transport(monkeypatch):
    from sniper.execution import evm_wallet
    from sniper.services.memecoin_evm_leg import _funded_evm_read_http
    calls = []

    def forbidden(*args, **kwargs):
        raise AssertionError('The default transport bypasses the safety deadline.')

    monkeypatch.setattr(evm_wallet, '_http', forbidden)

    def http(method, url, **kwargs):
        calls.append(method)
        return 200, {'price': '2000'}, None

    read_http = _funded_evm_read_http(http)
    state_fn, reserves_fn = _wired(state={
        'coin': COIN, 'curve': CURVE, 'counter': COUNTER_NATIVE, 'fee_bps': 100, 'state': 0})
    wrapped = C.wrap_token_safety_fn(
        '4663', 'rpc', http=read_http, taker=TAKER,
        fallback_fn=lambda *a, **k: (False, 'fallback'),
        state_fn=state_fn, reserves_fn=reserves_fn,
        safety_deadline_scope=read_http.safety_deadline)
    assert wrapped(COIN, usd=50)[0] is True
    assert calls == ['GET']


def test_the_curve_buy_and_sell_carry_their_own_gas_limit():
    """MEMECOIN_EVM_DEFAULT_ROUTE_GAS (900,000), replacing the signer's 500,000 default."""
    state_fn, reserves_fn = _wired(
        state={"coin": COIN, "curve": CURVE, "counter": COUNTER_NATIVE, "fee_bps": 100, "state": 0})
    buy = C.dex_launchpad_curve_quote(
        "4663", COUNTER_NATIVE, COIN, 10 ** 16, TAKER, rpc_url="https://rpc.example",
        slippage_bps=300, state_fn=state_fn, reserves_fn=reserves_fn)
    sell = C.dex_launchpad_curve_exit_quote(
        "4663", COIN, COUNTER_NATIVE, 10 ** 22, TAKER, rpc_url="https://rpc.example",
        slippage_bps=300, state_fn=state_fn, reserves_fn=reserves_fn)
    assert buy["transaction"]["gas"] == "900000" and sell["transaction"]["gas"] == "900000"


@pytest.mark.parametrize("name, value", [("MEMECOIN_EVM_DEFAULT_ROUTE_GAS", "900000"),
                                         ("MEMECOIN_EVM_KYBER_SELL_GAS", "5000000"),
                                         ("MEMECOIN_EVM_KYBER_BUY_GAS_MULT", "2.0")])
def test_an_unset_gas_knob_is_its_default_in_a_fresh_interpreter(name, value):
    """Read at import: a fresh interpreter with the variable absent."""
    import os
    import pathlib
    import subprocess
    import sys
    env = {k: v for k, v in os.environ.items() if k != name}
    out = subprocess.run(
        [sys.executable, "-c", "from sniper.settings import memecoin as m; print(m.%s)" % name],
        env=env, capture_output=True, text=True, timeout=60,
        cwd=str(pathlib.Path(__file__).resolve().parents[1]))
    assert out.returncode == 0, out.stderr[-400:]
    assert out.stdout.strip().splitlines()[-1] == value


@pytest.mark.parametrize("value", [0, -1])
def test_a_gas_knob_at_zero_never_signs_gas_zero(monkeypatch, value):
    """Gamma LOW on fdf0a8187: "gas": "0" is truthy, so the signer would sign a 0 limit. A knob at
    0 or below gives the default, on every builder that carries its own limit."""
    from sniper.execution import evm_flap_curve as F
    from sniper.execution import evm_kyber_quote as K
    from sniper.execution import evm_v2_exit_quote as V2
    from sniper.execution import evm_dex_quote as DQ
    from sniper.execution import evm_mixed_hop_router as MX
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_DEFAULT_ROUTE_GAS", value)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_FLAP_PORTAL_GAS", value)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_KYBER_SELL_GAS", value)
    for module in (C, V2, DQ, MX):
        assert module._route_gas() == 900_000, module.__name__
    assert F.portal_gas() == 1_500_000
    monkeypatch.setattr(K, "dex_kyber_quote", lambda *a, **k: {"transaction": {"gas": "900000"}})
    assert K.dex_kyber_exit_quote("4663", "0x1", "0x2", 1, "0x3")["transaction"]["gas"] == "5000000"
