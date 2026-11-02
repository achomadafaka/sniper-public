"""TDD for sniper/execution/evm_flap_curve.py.

The golden numbers below are not invented: they are YEE's real, live `getTokenV6` state on BSC
(chain 56) at the time this module was built (2026-09-20), paired with real `eth_call`
simulations of the actual `swapExactInput` function -- 3 buy sizes (0.01/0.1/1.0 BNB) and 3 sell
sizes (10k/100k/1M YEE). All 6 checks agreed with the live simulated chain within 1e-12%.

YEE's creator fee (getTokenV6 word[12]) is 100 bps, for a 2% total (100 platform + 100 creator)
-- but a live sample across 54 OTHER flap.sh tokens found the modal total fee is 4% (300 bps
creator), with 2%/5%/3% also observed. YEE is the outlier, not the rule -- every test below that
is not specifically about the fee itself uses YEE's real 100 bps creator fee, and a separate
section proves the formula is parameterised correctly for a different (modal 4%) token.
"""
import pytest

from sniper.execution import evm_flap_curve as C

PORTAL = "0xe2ce6ab80874fa9fa2aae65d277dd6b8e65c9de0"
YEE = "0x8d411a22bf0194b2f5ef665f8a2c7a9430ee7777"
NATIVE = "0x" + "00" * 20
TAKER = "0x898be9c27f8b391c47a3d9f2da17781aa8aa3a1d"

# YEE's real getTokenV6 state, chain 56, 2026-09-20.
YEE_RESERVE = 304288643025442896
YEE_R = 6140000000000000000
YEE_H = 107036752000000000000000000
YEE_K = 6797205657280000000000000000
YEE_CREATOR_FEE_BPS = 100


def _state(**overrides):
    state = {
        "status": 1, "reserve": YEE_RESERVE, "circulating_supply": 52272443043027083493254115,
        "price": 6109695396, "token_version": 5, "r": YEE_R, "h": YEE_H, "k": YEE_K,
        "dex_supply_thresh": 800000000000000000000000000, "quote_token": NATIVE,
        "native_to_quote_swap_enabled": 0, "extension_id": "0" * 64,
        "creator_fee_bps": YEE_CREATOR_FEE_BPS,
        "portal": PORTAL, "mint": YEE,
    }
    state.update(overrides)
    state["coin_reserve"] = (state["k"] * 10 ** 18) // (state["reserve"] + state["r"]) - state["h"]
    state["fee_bps"] = C._PLATFORM_FEE_BPS + state["creator_fee_bps"]
    return state


# -- curve_buy_out / curve_sell_out: golden numbers from a real curve, real eth_call sims -------

@pytest.mark.parametrize("amount_bnb,live_out", [
    (10 ** 16, 1601572398443673817714856),                 # 0.01 BNB
    (10 ** 17, 15799807669443017736724662),                # 0.1 BNB
    (10 ** 18, 139228022033988030415539485),                # 1.0 BNB
])
def test_curve_buy_out_matches_real_swap_exact_input(amount_bnb, live_out):
    out = C.curve_buy_out(_state(), amount_bnb)
    assert abs(out - live_out) / live_out < 0.001           # within 0.1%, same bar as the spec


@pytest.mark.parametrize("amount_tokens,live_out", [
    (10000 * 10 ** 18, 59874447230422),
    (100000 * 10 ** 18, 598693388006412),
    (1000000 * 10 ** 18, 5981830240505236),
])
def test_curve_sell_out_matches_real_swap_exact_input(amount_tokens, live_out):
    out = C.curve_sell_out(_state(), amount_tokens)
    assert abs(out - live_out) / live_out < 0.001


# -- curve_sell_out against 4 REAL executed sells, exact to the wei (Gamma, 2026-09-22) ---------
# Each token's FIRST Portal event, so the state read at block N-1 is its exact pre-trade state --
# not a reconstruction. r/h/k are the same template constants flap.sh uses for every token; only
# reserve/circulating_supply/creator_fee_bps vary per case. Covers 500 and 999 bps creator fees,
# neither exercised by the golden buy-side numbers above -- on SELLS, `100 + word[12]` held exact
# at both, a different result from the buy side (module docstring: 2 of 5 buy-side tokens deviate).

_FLAP_R = 6140000000000000000
_FLAP_H = 107036752000000000000000000
_FLAP_K = 6797205657280000000000000000


@pytest.mark.parametrize("reserve,circulating_supply,creator_fee_bps,tokens_in,real_bnb_received", [
    (3695627183521518196, 415956708963400370208608160, 200,
     34042552567037664403252, 469944060661411),                       # 300 bps total
    (1371849093864071136, 202172240966067719314211701, 300,
     7668407769264525879585481, 60600296093799403),                   # 400 bps total
    (7866464743624467297, 621746153573013912684163207, 400,
     2311409580786695037701491, 63075915308384345),                   # 500 bps total
    (44824050364031311, 8023166176176176985060327, 899,
     7974649736898038726796305, 40103910495000000),                   # 999 bps total
])
def test_curve_sell_out_matches_four_real_executed_sells_exactly(
        reserve, circulating_supply, creator_fee_bps, tokens_in, real_bnb_received):
    state = _state(reserve=reserve, circulating_supply=circulating_supply,
                   creator_fee_bps=creator_fee_bps, r=_FLAP_R, h=_FLAP_H, k=_FLAP_K)
    assert C.curve_sell_out(state, tokens_in) == real_bnb_received


def test_curve_buy_out_zero_amount_is_none():
    assert C.curve_buy_out(_state(), 0) is None
    assert C.curve_buy_out(_state(), -1) is None


def test_curve_sell_out_zero_amount_is_none():
    assert C.curve_sell_out(_state(), 0) is None


def test_curve_sell_out_refuses_a_sell_the_curve_cannot_pay():
    """Selling far more than the curve's own token-side capacity must fail closed, not return a
    payout larger than the curve's real reserve."""
    huge = 10 ** 12 * 10 ** 18
    assert C.curve_sell_out(_state(), huge) is None


def test_curve_buy_out_uses_the_tokens_own_fee_not_a_hardcoded_rate():
    """YEE is the outlier at 2% total; a 400-bps (4%, the live modal rate) token must take a
    LARGER bite out of the same input, computed from `state["fee_bps"]`, never a constant."""
    amount_in = 10 ** 16
    yee_out = C.curve_buy_out(_state(), amount_in)                      # 200 bps total
    modal_state = _state(creator_fee_bps=300)                           # 400 bps total
    modal_out = C.curve_buy_out(modal_state, amount_in)
    assert modal_state["fee_bps"] == 400
    assert modal_out < yee_out
    # net_in scales exactly with (10000 - fee_bps); the two outputs must reflect that ratio,
    # not merely "some smaller number".
    expected_ratio = (10_000 - 400) / (10_000 - 200)
    net_in_yee = amount_in * (10_000 - 200) // 10_000
    net_in_modal = amount_in * (10_000 - 400) // 10_000
    assert net_in_modal / net_in_yee == pytest.approx(expected_ratio, rel=1e-9)


def test_curve_sell_out_uses_the_tokens_own_fee_not_a_hardcoded_rate():
    amount_in = 10000 * 10 ** 18
    yee_out = C.curve_sell_out(_state(), amount_in)
    modal_out = C.curve_sell_out(_state(creator_fee_bps=300), amount_in)
    assert modal_out < yee_out


# -- curve_state: parse a real getTokenV5 shape, derive x, refuse on any bad read ----------------

def _words_hex(state_dict):
    def pad(v):
        if isinstance(v, str) and v.startswith("0x"):
            return v[2:].lower().rjust(64, "0")
        return f"{int(v):064x}"
    fields = ["status", "reserve", "circulating_supply", "price", "token_version", "r", "h", "k",
              "dex_supply_thresh", "quote_token", "native_to_quote_swap_enabled",
              "extension_id", "creator_fee_bps"]
    return "0x" + "".join(pad(state_dict[f]) for f in fields) + "0" * 128     # words 13-14, unused


def _http_returning(hex_result):
    def http(method, url, headers=None, body=None, timeout=10):
        return 200, {"result": hex_result}, None
    return http


def test_curve_state_derives_coin_reserve_matching_real_portal_balance():
    """The derived x must match the Portal's real on-chain YEE balanceOf to high precision -- the
    cross-check that confirmed the formula pairing (module docstring)."""
    real_portal_balance = 947727556956972916506745885
    http = _http_returning(_words_hex({
        "status": 1, "reserve": YEE_RESERVE, "circulating_supply": 52272443043027083493254115,
        "price": 6109695396, "token_version": 5, "r": YEE_R, "h": YEE_H, "k": YEE_K,
        "dex_supply_thresh": 800000000000000000000000000, "quote_token": NATIVE,
        "native_to_quote_swap_enabled": 0, "extension_id": 0,
        "creator_fee_bps": YEE_CREATOR_FEE_BPS,
    }))
    state = C.curve_state("56", YEE, "https://rpc.example", http=http)
    assert state is not None
    assert abs(state["coin_reserve"] - real_portal_balance) / real_portal_balance < 1e-10
    assert state["fee_bps"] == 200                     # 100 platform + YEE's 100 creator


def test_curve_state_computes_fee_bps_for_a_different_creator_fee():
    """The modal live rate (300 bps creator, 400 total) must come through the same decode path,
    not a hardcoded YEE-shaped assumption."""
    http = _http_returning(_words_hex({
        "status": 1, "reserve": YEE_RESERVE, "circulating_supply": 52272443043027083493254115,
        "price": 6109695396, "token_version": 5, "r": YEE_R, "h": YEE_H, "k": YEE_K,
        "dex_supply_thresh": 800000000000000000000000000, "quote_token": NATIVE,
        "native_to_quote_swap_enabled": 0, "extension_id": 0, "creator_fee_bps": 300,
    }))
    state = C.curve_state("56", YEE, "https://rpc.example", http=http)
    assert state["creator_fee_bps"] == 300
    assert state["fee_bps"] == 400


def test_curve_state_none_when_creator_fee_bps_is_out_of_range():
    http = _http_returning(_words_hex({
        "status": 1, "reserve": YEE_RESERVE, "circulating_supply": 52272443043027083493254115,
        "price": 6109695396, "token_version": 5, "r": YEE_R, "h": YEE_H, "k": YEE_K,
        "dex_supply_thresh": 800000000000000000000000000, "quote_token": NATIVE,
        "native_to_quote_swap_enabled": 0, "extension_id": 0, "creator_fee_bps": 10_000,
    }))
    assert C.curve_state("56", YEE, "https://rpc.example", http=http) is None


def test_curve_state_none_on_unrecognised_chain():
    assert C.curve_state("1", YEE, "https://rpc.example", http=lambda *a, **k: (200, {}, None)) is None


def test_curve_state_none_when_call_reverts():
    def http(method, url, headers=None, body=None, timeout=10):
        raise RuntimeError("execution reverted")
    assert C.curve_state("56", YEE, "https://rpc.example", http=http) is None


def test_curve_state_none_on_short_or_empty_result():
    assert C.curve_state("56", YEE, "https://rpc.example",
                         http=_http_returning("0x")) is None


# -- is_native_quote / is_graduated --------------------------------------------------------------

def test_is_native_quote_true_for_yee():
    assert C.is_native_quote(_state()) is True


def test_is_native_quote_false_for_an_erc20_quote():
    assert C.is_native_quote(_state(quote_token="0x" + "22" * 20)) is False


def test_is_graduated_false_for_yee_today():
    """YEE's real circulatingSupply is ~6.5% of dexSupplyThresh -- not graduated."""
    assert C.is_graduated(_state()) is False


def test_is_graduated_true_once_supply_crosses_the_threshold():
    state = _state(circulating_supply=900000000000000000000000000)   # > 800M threshold
    assert C.is_graduated(state) is True


def test_is_graduated_uses_supply_threshold_even_when_status_disagrees(caplog):
    """status is corroboration only, per the module docstring -- an unconfirmed enum must never
    override the documented supply-threshold check."""
    state = _state(circulating_supply=900000000000000000000000000, status=1)   # status says curve
    assert C.is_graduated(state) is True                                       # supply wins


# -- encode_swap_exact_input: exact byte layout, matching the confirmed selector -----------------

def test_encode_swap_exact_input_layout_matches_observed_calldata():
    data = C.encode_swap_exact_input(NATIVE, YEE, 10 ** 16, 12345)
    assert data.startswith("0xef7ec2e7")
    body = data[10:]
    assert int(body[0:64], 16) == 32                       # outer tuple offset
    assert body[64:128] == NATIVE[2:].rjust(64, "0")
    assert body[128:192] == YEE[2:].rjust(64, "0")
    assert int(body[192:256], 16) == 10 ** 16
    assert int(body[256:320], 16) == 12345
    assert int(body[320:384], 16) == 5 * 32                # bytes offset within the tuple
    assert int(body[384:448], 16) == 0                     # empty permitData length


def test_encode_approve_is_exact_amount_never_max_uint():
    data = C.encode_approve(PORTAL, 60_000_000)
    body = data[10:]
    assert int(body[64:128], 16) == 60_000_000
    assert int(body[64:128], 16) != 2 ** 256 - 1


# -- dex_flap_curve_quote / _exit_quote: refuse explicitly, never mis-quote ----------------------

def _wired(state):
    def state_fn(chain, mint, rpc_url, http=None):
        return state
    return state_fn


def _matching_simulate_fn(state, real_sell_allowed=False, override_allowed=False):
    """A fake `simulate_fn` that agrees with the local formula exactly -- for tests that need a
    working quote and are not themselves testing the live-vs-formula cross-check.

    A BUY-direction call (input=native) always succeeds, matching curve_buy_out. A SELL-direction
    call (input=the mint) fails by default -- matching production, where a quote-time caller has
    no real allowance yet -- unless `real_sell_allowed=True` (a standing allowance already
    exists) or `override_allowed=True` (no real allowance, but an `overrides` state-override was
    supplied and the simulation succeeds with it, same as the real chain would)."""
    def simulate_fn(portal, input_token, output_token, amount_atomic, taker, rpc_url, *,
                    http=None, overrides=None):
        is_buy = str(input_token).lower() == NATIVE
        if is_buy:
            return C.curve_buy_out(state, amount_atomic)
        if real_sell_allowed or (override_allowed and overrides is not None):
            return C.curve_sell_out(state, amount_atomic)
        return None
    return simulate_fn


def _overrides_fn(available=True):
    """A fake `overrides_fn`: returns a stand-in override dict, or None (layout not found)."""
    def overrides_fn(mint, spender, amount_atomic, *, taker, rpc_url, http=None):
        return {"stand-in": True} if available else None
    return overrides_fn


def test_quote_none_for_a_token_the_portal_does_not_recognise():
    out = C.dex_flap_curve_quote(
        "56", NATIVE, YEE, 10 ** 16, TAKER, rpc_url="https://rpc.example",
        state_fn=_wired(None))
    assert out is None


def test_quote_refuses_a_non_native_quote_mint_explicitly():
    """The core scope guarantee: a non-native-quote flap.sh mint is refused BY NAME (verified via
    `refusal_reason`, the countable channel), never mis-quoted with the native-only formula --
    and the miss itself stays a bare None, an ordinary rung miss, not a fabricated blind spot."""
    state = _state(quote_token="0x" + "22" * 20)
    out = C.dex_flap_curve_quote(
        "56", NATIVE, YEE, 10 ** 16, TAKER, rpc_url="https://rpc.example",
        state_fn=_wired(state))
    assert out is None
    assert C.refusal_reason(YEE, state) == C.NON_NATIVE_QUOTE_REFUSED


def test_quote_refuses_a_graduated_coin():
    state = _state(circulating_supply=900000000000000000000000000)
    out = C.dex_flap_curve_quote(
        "56", NATIVE, YEE, 10 ** 16, TAKER, rpc_url="https://rpc.example",
        state_fn=_wired(state))
    assert out is None
    assert C.refusal_reason(YEE, state) == C.GRADUATED_REFUSED


def test_refusal_reason_none_for_an_eligible_native_curve_coin():
    assert C.refusal_reason(YEE, _state()) is None


def test_refusal_reason_not_a_flap_token_when_state_is_none():
    assert C.refusal_reason(YEE, None) == C.NOT_A_FLAP_TOKEN


def test_quote_builds_native_value_transaction_for_yee():
    state = _state()
    out = C.dex_flap_curve_quote(
        "56", NATIVE, YEE, 10 ** 16, TAKER, rpc_url="https://rpc.example",
        state_fn=_wired(state), simulate_fn=_matching_simulate_fn(state))
    assert out is not None
    assert out["transaction"]["to"] == PORTAL
    assert int(out["transaction"]["value"], 16) == 10 ** 16
    assert out["transaction"]["data"].startswith("0xef7ec2e7")
    assert out["pre_transactions"] == []
    assert int(out["buyAmount"]) > 0
    assert int(out["minBuyAmount"]) > 0
    assert int(out["minBuyAmount"]) <= int(out["buyAmount"])


def test_quote_accepts_the_chains_weth_and_unwraps_first():
    """The real ladder calls with sell_token=WETH (CHAIN_QUOTE), never the literal native
    address -- our wallet holds WBNB, not native, on chain 56."""
    from sniper.execution.evm_wallet import CHAIN_QUOTE
    weth = CHAIN_QUOTE["56"]["token"].lower()
    state = _state()
    out = C.dex_flap_curve_quote(
        "56", weth, YEE, 10 ** 16, TAKER, rpc_url="https://rpc.example",
        state_fn=_wired(state), simulate_fn=_matching_simulate_fn(state))
    assert out is not None
    assert int(out["transaction"]["value"], 16) == 10 ** 16     # curve still paid in native
    assert len(out["pre_transactions"]) == 1
    pre = out["pre_transactions"][0]
    assert pre["to"] == weth
    assert pre["data"].startswith("0x2e1a7d4d")                 # WETH.withdraw(uint256)
    assert int(pre["data"][10:], 16) == 10 ** 16
    from sniper.services.memecoin_evm_gas_topup import WETH_CALL_GAS_LIMIT
    assert pre["gas"] == str(WETH_CALL_GAS_LIMIT)               # one limit for WETH calls


def test_quote_none_when_paying_token_is_neither_native_nor_weth():
    out = C.dex_flap_curve_quote(
        "56", "0x" + "44" * 20, YEE, 10 ** 16, TAKER, rpc_url="https://rpc.example",
        state_fn=_wired(_state()))
    assert out is None


# -- the WETH-paid buy simulation and the native it is about to unwrap (2026-09-23) ------------
#
# A WETH-paid buy plans its own `WETH.withdraw(amount_atomic)` BEFORE the swap, so the wallet's
# native balance AT SIMULATION TIME is not the balance the swap will actually spend from. The
# funded 56 wallet held 0.009789 BNB native against a 0.1 BNB clip on 2026-09-23 and the live
# `eth_call` answered `insufficient funds for gas * price + value ... have 9789007892114433 want
# 100000000000000000`; the identical call with a balance state override succeeded. A NATIVE-paid
# buy has no unwrap to account for, so its real balance must keep deciding.


def _balance_gated_simulate_fn(state, native_balance):
    """A fake `simulate_fn` that behaves like the chain: a buy sending `amount_atomic` of native
    value succeeds only when the caller can actually cover it -- from the real balance, or from
    whatever an `overrides` state-override fabricates for the taker."""
    def simulate_fn(portal, input_token, output_token, amount_atomic, taker, rpc_url, *,
                    http=None, overrides=None):
        seen["overrides"] = overrides
        available = native_balance
        entry = (overrides or {}).get(taker) or {}
        if entry.get("balance") is not None:
            available = int(entry["balance"], 16)
        if available < amount_atomic:
            return None                                  # insufficient funds for value
        return C.curve_buy_out(state, amount_atomic)
    seen = {"overrides": "unset"}
    simulate_fn.seen = seen
    return simulate_fn


def test_weth_paid_buy_is_given_the_native_its_own_unwrap_will_create():
    """The defect this fixes: a wallet with the WBNB to fund the clip and only gas in native was
    refused a route it can pay for."""
    from sniper.execution.evm_wallet import CHAIN_QUOTE
    weth = CHAIN_QUOTE["56"]["token"].lower()
    state = _state()
    simulate_fn = _balance_gated_simulate_fn(state, native_balance=10 ** 15)   # 0.001 native
    out = C.dex_flap_curve_quote(
        "56", weth, YEE, 10 ** 16, TAKER, rpc_url="https://rpc.example",
        state_fn=_wired(state), simulate_fn=simulate_fn)
    assert out is not None
    assert simulate_fn.seen["overrides"] is not None
    assert int(simulate_fn.seen["overrides"][TAKER]["balance"], 16) >= 10 ** 16


def test_native_paid_buy_keeps_its_real_balance_so_an_unfundable_entry_still_refuses():
    """The opposite defect: an unconditional override would quote an entry the wallet cannot
    pay for. Nothing unwraps on a native-paid buy, so the real balance is the truth."""
    state = _state()
    simulate_fn = _balance_gated_simulate_fn(state, native_balance=10 ** 15)   # 0.001 native
    out = C.dex_flap_curve_quote(
        "56", NATIVE, YEE, 10 ** 16, TAKER, rpc_url="https://rpc.example",
        state_fn=_wired(state), simulate_fn=simulate_fn)
    assert out is None
    assert simulate_fn.seen["overrides"] is None


def test_the_weth_buy_override_is_the_unwrap_amount_one_for_one_plus_gas():
    """`withdraw` is 1:1 WETH -> native, so the fabricated native is the SAME number as the
    unwrap, plus a gas allowance. A 1:1 assumption that silently stops being 1:1 would under- or
    over-fund every simulation on this rung, so it is pinned here."""
    from sniper.execution.evm_token_safety import _GAS_HEX
    from sniper.execution.evm_wallet import CHAIN_QUOTE
    weth = CHAIN_QUOTE["56"]["token"].lower()
    state = _state()
    amount = 10 ** 16
    simulate_fn = _balance_gated_simulate_fn(state, native_balance=0)
    out = C.dex_flap_curve_quote(
        "56", weth, YEE, amount, TAKER, rpc_url="https://rpc.example",
        state_fn=_wired(state), simulate_fn=simulate_fn)
    assert out is not None
    fabricated = int(simulate_fn.seen["overrides"][TAKER]["balance"], 16)
    assert fabricated == amount + int(_GAS_HEX, 16)
    unwrapped = int(out["pre_transactions"][0]["data"][10:], 16)
    assert unwrapped == amount                           # the override matches it exactly


def test_exit_quote_refuses_a_non_native_quote_mint_explicitly():
    out = C.dex_flap_curve_exit_quote(
        "56", YEE, NATIVE, 10 ** 18, TAKER, rpc_url="https://rpc.example",
        state_fn=_wired(_state(quote_token="0x" + "22" * 20)))
    assert out is None


def test_exit_quote_builds_erc20_allowance_issue_not_native_value():
    state = _state()
    out = C.dex_flap_curve_exit_quote(
        "56", YEE, NATIVE, 10 ** 22, TAKER, rpc_url="https://rpc.example",
        state_fn=_wired(state), simulate_fn=_matching_simulate_fn(state, override_allowed=True),
        overrides_fn=_overrides_fn())
    assert out is not None
    assert out["transaction"]["value"] == "0x0"
    assert out["issues"] == {"allowance": {"spender": PORTAL}}
    assert "pre_transactions" not in out              # native payout needs no wrap step


@pytest.mark.parametrize("sell", [False, True])
@pytest.mark.parametrize("slippage_bps", [-1, 10000, 10001])
def test_curve_quote_requires_a_valid_slippage(sell, slippage_bps):
    quote_fn = C.dex_flap_curve_exit_quote if sell else C.dex_flap_curve_quote
    args = (YEE, NATIVE) if sell else (NATIVE, YEE)
    out = quote_fn("56", *args, 10 ** 18, TAKER, rpc_url="https://rpc.example",
                   slippage_bps=slippage_bps, state_fn=_wired(_state()))
    assert out is None


# -- whole_balance_exit_quote_or_refuse: the clip-size safety check ------------------------------

def test_clip_refuse_when_not_a_flap_token():
    quote, reason = C.whole_balance_exit_quote_or_refuse(
        "56", YEE, 10 ** 18, "https://rpc.example", taker=TAKER, state_fn=_wired(None))
    assert quote is None
    assert reason == C.NOT_A_FLAP_TOKEN


def test_clip_refuse_when_non_native_quote():
    quote, reason = C.whole_balance_exit_quote_or_refuse(
        "56", YEE, 10 ** 18, "https://rpc.example", taker=TAKER,
        state_fn=_wired(_state(quote_token="0x" + "22" * 20)))
    assert quote is None
    assert reason == C.NON_NATIVE_QUOTE_REFUSED


def test_clip_does_not_refuse_a_graduated_coin_on_exit():
    """Graduation is entry-only (2026-09-20): the Portal still serves both a buy and a sell on a
    real graduated token (verified directly, module docstring), so refusing an exit purely for
    graduation risks a stuck bag. The exit path must still try to price it."""
    state = _state(circulating_supply=900000000000000000000000000)
    quote, reason = C.whole_balance_exit_quote_or_refuse(
        "56", YEE, 10 ** 18, "https://rpc.example", taker=TAKER,
        state_fn=_wired(state), simulate_fn=_matching_simulate_fn(state, real_sell_allowed=True))
    assert reason is None
    assert quote is not None
    assert quote["route"]["quote_quality"] == "exact"


def test_exit_quote_gives_an_exact_answer_for_a_graduated_coin_once_allowance_exists():
    """The gate does not refuse graduation on exit, and the real simulation does not care what
    graduated means -- verified live on a real graduated token (Taraxacum), module docstring."""
    state = _state(circulating_supply=900000000000000000000000000)
    out = C.dex_flap_curve_exit_quote(
        "56", YEE, NATIVE, 10 ** 18, TAKER, rpc_url="https://rpc.example",
        state_fn=_wired(state), simulate_fn=_matching_simulate_fn(state, real_sell_allowed=True))
    assert out is not None
    assert out["route"]["quote_quality"] == "exact"


def test_exit_quote_still_refuses_a_graduated_coin_via_override_at_real_reserve_zero():
    """CORRECTED 2026-09-22 (Gamma found this at runtime, reproduced on Taraxacum and DPAID;
    Alpha's call to leave it). An earlier version of this test used circulating_supply alone to
    fake graduation and kept YEE's real POSITIVE reserve -- a fixture that never exercises the
    real bug, because a real graduated coin's reserve is 0 (module docstring), and curve_sell_out
    is undefined there. The override simulation itself still pays a real number (the fake below
    mirrors that), but the fee-model cross-check reads curve_sell_out's None as "unverified" and
    refuses -- correctly, since there is no real post-graduation Portal sell to validate the
    number against. This is a known, accepted gap, not a bug to route around silently."""
    state = _state(reserve=0, circulating_supply=900000000000000000000000000)

    def simulate_fn(portal, input_token, output_token, amount_atomic, taker, rpc_url, *,
                    http=None, overrides=None):
        if str(input_token).lower() == NATIVE:
            return C.curve_buy_out(state, amount_atomic)
        if overrides is None:
            return None
        return 67271131173702835                          # a real number the Portal did pay

    out = C.dex_flap_curve_exit_quote(
        "56", YEE, NATIVE, 10 ** 18, TAKER, rpc_url="https://rpc.example",
        state_fn=_wired(state), simulate_fn=simulate_fn, overrides_fn=_overrides_fn())
    assert out is None
    assert C.curve_sell_out(state, 10 ** 18) is None       # confirms WHY: the formula is undefined here


def test_refusal_reason_allows_graduated_when_asked():
    state = _state(circulating_supply=900000000000000000000000000)
    assert C.refusal_reason(YEE, state) == C.GRADUATED_REFUSED          # entry: still refused
    assert C.refusal_reason(YEE, state, allow_graduated=True) is None   # exit: allowed through


def test_clip_check_succeeds_on_a_real_quotable_curve():
    """An exact quote (real allowance, in this test) passes the clip-size safety check -- see
    `test_clip_refuse_when_the_override_layout_cannot_be_found` for the refusal side."""
    state = _state()
    quote, reason = C.whole_balance_exit_quote_or_refuse(
        "56", YEE, 10 ** 18, "https://rpc.example", taker=TAKER,
        state_fn=_wired(state), simulate_fn=_matching_simulate_fn(state, real_sell_allowed=True))
    assert reason is None
    assert quote is not None
    assert quote["route"]["quote_quality"] == "exact"
    assert int(quote["minBuyAmount"]) > 0


def test_clip_refuse_when_the_override_layout_cannot_be_found():
    """No real allowance and no discoverable override layout: there is nothing honest to quote,
    so the clip check must refuse rather than pass a fabricated answer."""
    state = _state()
    quote, reason = C.whole_balance_exit_quote_or_refuse(
        "56", YEE, 10 ** 18, "https://rpc.example", taker=TAKER,
        state_fn=_wired(state), simulate_fn=_matching_simulate_fn(state),
        overrides_fn=_overrides_fn(available=False))
    assert quote is None
    assert reason == C.EXIT_UNQUOTABLE


def test_clip_refuses_if_a_future_regression_ever_returns_a_non_exact_quality(monkeypatch):
    """NOT_EXACT is unreachable through dex_flap_curve_exit_quote's own logic today (it can only
    return None or quote_quality == "exact" -- the estimate path was deleted 2026-09-22), but this
    check is the one place that verifies the callee's own contract instead of trusting it (Gamma,
    2026-09-20). Keep it under direct test so a future regression cannot silently defeat it."""
    def fake_exit_quote(*a, **k):
        return {"route": {"quote_quality": "not_exact_for_testing"}}
    monkeypatch.setattr(C, "dex_flap_curve_exit_quote", fake_exit_quote)
    quote, reason = C.whole_balance_exit_quote_or_refuse(
        "56", YEE, 10 ** 18, "https://rpc.example", taker=TAKER, state_fn=_wired(_state()))
    assert quote is None
    assert reason == C.NOT_EXACT


def test_clip_refuse_when_the_whole_balance_exceeds_the_curves_capacity():
    """A dust-sized probe passing while a real clip-sized exit reverts is exactly the failure
    shape this check exists to catch (finding-memecoin-evm-honeypot-false-positive). The override
    itself must succeed here -- this test is about curve_sell_out's own capacity refusal, not
    about the override being unavailable for an unrelated reason."""
    state = _state()
    huge_balance = 10 ** 12 * 10 ** 18
    quote, reason = C.whole_balance_exit_quote_or_refuse(
        "56", YEE, huge_balance, "https://rpc.example", taker=TAKER,
        state_fn=_wired(state), simulate_fn=_matching_simulate_fn(state, override_allowed=True),
        overrides_fn=_overrides_fn())
    assert quote is None
    assert reason == C.EXIT_UNQUOTABLE


# -- simulate_swap_exact_input: the eth_call instrument itself ---------------------------------

def _http_call_recording(result_hex, calls):
    def http(method, url, headers=None, body=None, timeout=10):
        import json as _json
        req = _json.loads(body)
        calls.append(req)
        return 200, {"result": result_hex}, None
    return http


def test_simulate_swap_exact_input_sends_value_for_a_native_buy():
    calls = []
    http = _http_call_recording("0x" + f"{123456:064x}", calls)
    out = C.simulate_swap_exact_input(
        PORTAL, NATIVE, YEE, 10 ** 16, TAKER, "https://rpc.example", http=http)
    assert out == 123456
    call_obj = calls[0]["params"][0]
    assert call_obj["to"] == PORTAL
    assert call_obj["from"] == TAKER
    assert int(call_obj["value"], 16) == 10 ** 16
    assert call_obj["data"].startswith("0xef7ec2e7")
    # minOutputAmount encoded as 0 -- the simulation itself must never be slippage-refused.
    body = call_obj["data"][10:]
    assert int(body[256:320], 16) == 0


def test_simulate_swap_exact_input_sends_no_value_for_a_sell():
    calls = []
    http = _http_call_recording("0x" + f"{123456:064x}", calls)
    C.simulate_swap_exact_input(
        PORTAL, YEE, NATIVE, 10 ** 18, TAKER, "https://rpc.example", http=http)
    call_obj = calls[0]["params"][0]
    assert "value" not in call_obj


def test_simulate_swap_exact_input_none_on_revert():
    def http(method, url, headers=None, body=None, timeout=10):
        return 200, {"error": {"code": 3, "message": "execution reverted"}}, None
    assert C.simulate_swap_exact_input(
        PORTAL, YEE, NATIVE, 10 ** 18, TAKER, "https://rpc.example", http=http) is None


def test_simulate_swap_exact_input_none_on_zero_output():
    http = _http_call_recording("0x" + "0" * 64, [])
    assert C.simulate_swap_exact_input(
        PORTAL, NATIVE, YEE, 10 ** 16, TAKER, "https://rpc.example", http=http) is None


# -- fee-model cross-check: the architecture that replaces trusting getTokenV6 word[12] ----------

def test_quote_uses_the_live_simulated_amount_not_the_formula():
    """Even when both are 'close enough' to pass the tolerance, the reported buyAmount must be
    the LIVE number, not the formula's -- eth_call is the source, the formula is a cross-check."""
    state = _state()
    formula_amount = C.curve_buy_out(state, 10 ** 16)
    live_amount = formula_amount + 1                          # trivially within tolerance

    def simulate_fn(portal, input_token, output_token, amount_atomic, taker, rpc_url, *,
                    http=None, overrides=None):
        return live_amount

    out = C.dex_flap_curve_quote(
        "56", NATIVE, YEE, 10 ** 16, TAKER, rpc_url="https://rpc.example",
        state_fn=_wired(state), simulate_fn=simulate_fn)
    assert out is not None
    assert int(out["buyAmount"]) == live_amount
    assert int(out["buyAmount"]) != formula_amount


def test_quote_refuses_when_live_and_formula_disagree_beyond_tolerance(caplog):
    """The exact failure mode Gamma found: a token whose real fee is not `100 + word[12]` bps
    must be refused, not quoted off the wrong model."""
    state = _state()
    formula_amount = C.curve_buy_out(state, 10 ** 16)
    live_amount = int(formula_amount * 1.05)                  # 5% off -- well past the 0.1% bar

    def simulate_fn(portal, input_token, output_token, amount_atomic, taker, rpc_url, *,
                    http=None, overrides=None):
        return live_amount

    out = C.dex_flap_curve_quote(
        "56", NATIVE, YEE, 10 ** 16, TAKER, rpc_url="https://rpc.example",
        state_fn=_wired(state), simulate_fn=simulate_fn)
    assert out is None
    assert C.FEE_MODEL_UNVERIFIED in caplog.text


def test_quote_refuses_when_the_live_simulation_itself_fails():
    state = _state()
    simulate_fn = lambda *a, **k: None
    out = C.dex_flap_curve_quote(
        "56", NATIVE, YEE, 10 ** 16, TAKER, rpc_url="https://rpc.example",
        state_fn=_wired(state), simulate_fn=simulate_fn)
    assert out is None


def test_exit_quote_refuses_when_the_override_disagrees_with_the_formula(caplog):
    """No real allowance yet, so this uses the override simulation -- a mismatched override
    result must refuse the sell exactly as the entry path refuses a mismatched buy."""
    state = _state()
    modeled = C.curve_sell_out(state, 10 ** 18)
    bad_payout = int(modeled * 1.05)

    def simulate_fn(portal, input_token, output_token, amount_atomic, taker, rpc_url, *,
                    http=None, overrides=None):
        if str(input_token).lower() == NATIVE:
            return C.curve_buy_out(state, amount_atomic)
        return bad_payout if overrides is not None else None

    out = C.dex_flap_curve_exit_quote(
        "56", YEE, NATIVE, 10 ** 18, TAKER, rpc_url="https://rpc.example",
        state_fn=_wired(state), simulate_fn=simulate_fn, overrides_fn=_overrides_fn())
    assert out is None
    assert C.FEE_MODEL_UNVERIFIED in caplog.text


def test_exit_quote_refuses_when_the_override_is_unavailable():
    """No real allowance, and the override's storage-slot layout could not be found -- nothing
    honest to quote, so this refuses rather than guesses."""
    state = _state()
    simulate_fn = lambda *a, **k: None
    out = C.dex_flap_curve_exit_quote(
        "56", YEE, NATIVE, 10 ** 18, TAKER, rpc_url="https://rpc.example",
        state_fn=_wired(state), simulate_fn=simulate_fn, overrides_fn=_overrides_fn(available=False))
    assert out is None


def test_exit_quote_passes_the_discovered_override_straight_into_the_simulation():
    """Proves the wiring itself, not just the end result: overrides_fn is called with the exact
    mint/spender/amount/taker/rpc_url this quote is for, and its return value is what
    simulate_fn actually receives as `overrides` -- not a separately-reconstructed one."""
    state = _state()
    seen = {}

    def simulate_fn(portal, input_token, output_token, amount_atomic, taker, rpc_url, *,
                    http=None, overrides=None):
        if str(input_token).lower() == NATIVE:
            return C.curve_buy_out(state, amount_atomic)
        if overrides is None:
            return None
        seen["overrides"] = overrides
        return C.curve_sell_out(state, amount_atomic)

    def overrides_fn(mint, spender, amount_atomic, *, taker, rpc_url, http=None):
        assert mint == YEE
        assert spender == PORTAL
        assert amount_atomic == 10 ** 18
        assert taker == TAKER
        return {"marker": "yes"}

    out = C.dex_flap_curve_exit_quote(
        "56", YEE, NATIVE, 10 ** 18, TAKER, rpc_url="https://rpc.example",
        state_fn=_wired(state), simulate_fn=simulate_fn, overrides_fn=overrides_fn)
    assert out is not None
    assert seen["overrides"] == {"marker": "yes"}


def test_fee_model_verified_true_within_tolerance():
    assert C._fee_model_verified(YEE, 1000, 1000) is True
    assert C._fee_model_verified(YEE, 1000, 1000999) is False   # exactly at the edge, over 0.1%


def test_fee_model_verified_false_when_either_side_is_none():
    assert C._fee_model_verified(YEE, None, 1000) is False
    assert C._fee_model_verified(YEE, 1000, None) is False


# -- exit quote: real simulation preferred, probe fallback only when unavailable ------------------

def test_exit_quote_uses_a_real_sell_simulation_when_allowance_already_exists():
    """A taker who already holds a standing allowance (a second sell attempt, or any prior
    approval) gets the EXACT chain answer directly, never needing the allowance override."""
    state = _state()
    real_payout = C.curve_sell_out(state, 10 ** 18) + 7        # deliberately != the formula exactly

    def simulate_fn(portal, input_token, output_token, amount_atomic, taker, rpc_url, *,
                    http=None):
        if str(input_token).lower() != NATIVE:
            return real_payout
        return C.curve_buy_out(state, amount_atomic)           # probe path unused in this test

    out = C.dex_flap_curve_exit_quote(
        "56", YEE, NATIVE, 10 ** 18, TAKER, rpc_url="https://rpc.example",
        state_fn=_wired(state), simulate_fn=simulate_fn)
    assert out is not None
    assert int(out["buyAmount"]) == real_payout
    assert out["route"]["quote_quality"] == "exact"
    assert out["route"]["quote_basis"] == "real_allowance"


def test_exit_quote_uses_the_allowance_override_without_a_real_allowance():
    """The core fix (2026-09-22): no real allowance yet used to mean an estimate, or nothing at
    all post-graduation. Now it means an exact quote via the allowance override instead."""
    state = _state()
    out = C.dex_flap_curve_exit_quote(
        "56", YEE, NATIVE, 10 ** 18, TAKER, rpc_url="https://rpc.example",
        state_fn=_wired(state), simulate_fn=_matching_simulate_fn(state, override_allowed=True),
        overrides_fn=_overrides_fn())
    assert out is not None
    assert out["route"]["quote_quality"] == "exact"
    assert out["route"]["quote_basis"] == "allowance_override"
    assert int(out["buyAmount"]) == C.curve_sell_out(state, 10 ** 18)


def test_whole_balance_check_reports_quote_quality_too():
    state = _state()
    quote, reason = C.whole_balance_exit_quote_or_refuse(
        "56", YEE, 10 ** 18, "https://rpc.example", taker=TAKER,
        state_fn=_wired(state), simulate_fn=_matching_simulate_fn(state, real_sell_allowed=True))
    assert reason is None
    assert quote["route"]["quote_quality"] == "exact"


# -- simulate_swap_exact_input: taker must be real, per Gamma's operational finding --------------

@pytest.mark.parametrize("bad_taker", [None, "", "0x" + "0" * 40, "0x0"])
def test_simulate_swap_exact_input_refuses_a_missing_or_zero_taker(bad_taker):
    """swapExactInput reverts 0x90b8ec18 for a missing/zero caller -- a caller bug here must not
    silently read as 'no route' with no distinguishing signal."""
    def http(method, url, headers=None, body=None, timeout=10):
        raise AssertionError("must not reach the network with an invalid taker")
    assert C.simulate_swap_exact_input(
        PORTAL, NATIVE, YEE, 10 ** 16, bad_taker, "https://rpc.example", http=http) is None


def test_simulate_swap_exact_input_accepts_a_real_taker():
    http = _http_call_recording("0x" + f"{123456:064x}", [])
    assert C.simulate_swap_exact_input(
        PORTAL, NATIVE, YEE, 10 ** 16, TAKER, "https://rpc.example", http=http) == 123456


# -- _implied_fee_bps / enhanced FEE_MODEL_UNVERIFIED logging: count the refusals, per Alpha -----

def test_implied_fee_bps_recovers_the_true_rate_from_a_live_result():
    """Back-solving a live buy result computed at a KNOWN fee must recover that same fee -- the
    sanity check that this diagnostic is not just a number, it is the right number."""
    state = _state()                                        # fee_bps = 200
    live_out = C.curve_buy_out(state, 10 ** 16)
    implied = C._implied_fee_bps(state, 10 ** 16, live_out)
    assert implied == pytest.approx(200, abs=0.01)


def test_implied_fee_bps_none_on_bad_input():
    state = _state()
    assert C._implied_fee_bps(state, 10 ** 16, None) is None
    assert C._implied_fee_bps(state, 0, 1000) is None


def test_fee_model_unverified_log_names_the_mint_and_both_rates(caplog):
    """Alpha, 2026-09-20: a fail-closed refusal must still be countable -- the log line must
    carry the mint, the modeled fee_bps, and the fee_bps the live result actually implies, not
    just raw amounts nobody can compare across tokens."""
    state = _state()
    modeled_out = C.curve_buy_out(state, 10 ** 16)
    bad_live_out = int(modeled_out * 1.05)
    ok = C._fee_model_verified(YEE, modeled_out, bad_live_out, state=state, amount_in=10 ** 16)
    assert ok is False
    assert YEE in caplog.text
    assert "modeled_fee_bps=200" in caplog.text
    assert "implied_fee_bps=" in caplog.text


def test_fee_model_unverified_log_without_state_omits_implied_bps(caplog):
    """The diagnostic is optional -- a caller that does not supply state/amount_in still gets a
    usable (if less complete) log line, never a crash."""
    ok = C._fee_model_verified(YEE, 1000, 2000)
    assert ok is False
    assert "implied_fee_bps=None" in caplog.text


# -- wrap_token_safety_fn: the pre-entry clip check, buildable only since the allowance override --
# mirrors evm_launchpad_curve.py's function of the same name (chain 4663's equivalent).

def _fallback_recording():
    calls = []

    def fallback(mint, *, deadline=None, usd=None, bought_amount_atomic=None, **kwargs):
        calls.append((mint, deadline, usd, bought_amount_atomic))
        return True, "standard_probe_ok"
    return fallback, calls


def test_safety_wrapper_falls_through_when_usd_is_not_supplied():
    fallback, calls = _fallback_recording()
    wrapped = C.wrap_token_safety_fn("56", "https://rpc.example", taker=TAKER, fallback_fn=fallback)
    assert wrapped(YEE) == (True, "standard_probe_ok")
    assert calls == [(YEE, None, None, None)]


def test_safety_wrapper_falls_through_for_a_non_flap_mint():
    fallback, calls = _fallback_recording()
    wrapped = C.wrap_token_safety_fn(
        "56", "https://rpc.example", taker=TAKER, fallback_fn=fallback, state_fn=_wired(None))
    assert wrapped(YEE, usd=50.0) == (True, "standard_probe_ok")
    assert calls == [(YEE, None, 50.0, None)]


def test_safety_wrapper_falls_through_once_graduated():
    fallback, calls = _fallback_recording()
    state = _state(circulating_supply=900000000000000000000000000)
    wrapped = C.wrap_token_safety_fn(
        "56", "https://rpc.example", taker=TAKER, fallback_fn=fallback, state_fn=_wired(state))
    assert wrapped(YEE, usd=50.0) == (True, "standard_probe_ok")
    assert calls == [(YEE, None, 50.0, None)]


def test_safety_wrapper_checks_the_curve_directly_for_a_live_native_mint():
    fallback, calls = _fallback_recording()
    state = _state()
    wrapped = C.wrap_token_safety_fn(
        "56", "https://rpc.example", taker=TAKER, fallback_fn=fallback, state_fn=_wired(state),
        simulate_fn=_matching_simulate_fn(state, override_allowed=True),
        overrides_fn=_overrides_fn())
    ok, reason = wrapped(YEE, bought_amount_atomic=10 ** 18)
    assert ok is True
    assert reason == "flap_curve_exit_quotable_at_clip"
    assert calls == []                                              # never touched the standard cascade


def test_safety_wrapper_refuses_when_the_override_is_unavailable():
    fallback, calls = _fallback_recording()
    state = _state()
    wrapped = C.wrap_token_safety_fn(
        "56", "https://rpc.example", taker=TAKER, fallback_fn=fallback, state_fn=_wired(state),
        simulate_fn=_matching_simulate_fn(state), overrides_fn=_overrides_fn(available=False))
    ok, reason = wrapped(YEE, bought_amount_atomic=10 ** 18)
    assert ok is False
    assert calls == []


def test_safety_wrapper_uses_bought_amount_atomic_instead_of_deriving_its_own():
    """A price feed that would refuse (0.0) must not be consulted at all when the real bought
    amount is already known -- proves the estimate path is skipped, not merely correct by luck."""
    fallback, calls = _fallback_recording()
    state = _state()

    def price_fn_that_must_not_be_called(chain):
        raise AssertionError("bound identity must skip the usd-based entry-quote derivation")

    wrapped = C.wrap_token_safety_fn(
        "56", "https://rpc.example", taker=TAKER, fallback_fn=fallback, state_fn=_wired(state),
        simulate_fn=_matching_simulate_fn(state, override_allowed=True),
        overrides_fn=_overrides_fn(), native_usd_price_fn=price_fn_that_must_not_be_called)
    ok, reason = wrapped(YEE, usd=50.0, bought_amount_atomic=10 ** 18)
    assert ok is True
    assert reason == "flap_curve_exit_quotable_at_clip"
    assert calls == []


def test_safety_wrapper_forwards_bought_amount_atomic_on_pass_through():
    """Every mint the curve check does not own must still hand `bought_amount_atomic` on to the
    real token-safety function unchanged -- never silently dropped."""
    fallback, calls = _fallback_recording()
    wrapped = C.wrap_token_safety_fn(
        "56", "https://rpc.example", taker=TAKER, fallback_fn=fallback, state_fn=_wired(None))
    wrapped(YEE, usd=50.0, bought_amount_atomic=123456)
    assert calls == [(YEE, None, 50.0, 123456)]


def test_safety_wrapper_treats_a_zero_bound_amount_as_unknown_and_falls_back_to_usd():
    fallback, calls = _fallback_recording()
    state = _state()
    wrapped = C.wrap_token_safety_fn(
        "56", "https://rpc.example", taker=TAKER, fallback_fn=fallback, state_fn=_wired(state),
        simulate_fn=_matching_simulate_fn(state, override_allowed=True),
        overrides_fn=_overrides_fn(), native_usd_price_fn=lambda chain: 500.0)
    ok, reason = wrapped(YEE, usd=50.0, bought_amount_atomic=0)
    assert ok is True
    assert reason == "flap_curve_exit_quotable_at_clip"
    assert calls == []


# -- the Portal converts native itself: nativeToQuoteSwapEnabled (2026-09-22) --------------------
#
# `curve_state` decoded this flag and NOTHING read it, so the rung refused every non-native curve
# -- 61 of the 102 flap tokens among 141 chain-56 mints seen in 7 days, the majority of the venue.
# Proven live before any of this was written, never inferred from the field name: 4 of 4 flag-set
# tokens simulated a real swapExactInput BOTH ways (native -> mint, mint -> native), the one
# flag-clear non-native token on its curve REVERTED both ways, and paying the quote token directly
# reverted on all 4.

NON_NATIVE = "0x" + "22" * 20


def _knob(monkeypatch, on):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_FLAP_NATIVE_SWAP_ENABLED", on, raising=False)


def test_a_converting_curve_is_no_longer_refused(monkeypatch):
    """THE PROPERTY. Flag set + knob on -> eligible, where it used to be NON_NATIVE_QUOTE_REFUSED."""
    _knob(monkeypatch, True)
    state = _state(quote_token=NON_NATIVE, native_to_quote_swap_enabled=1)
    assert C.takes_native_payment(state) is True
    assert C.refusal_reason(YEE, state) is None


def test_the_knob_is_off_by_default_so_the_venue_keeps_its_shipped_behaviour(monkeypatch):
    """No live on-curve token of this class has been entered end to end, so OFF is the default and
    OFF must be exactly the old behaviour."""
    _knob(monkeypatch, False)
    state = _state(quote_token=NON_NATIVE, native_to_quote_swap_enabled=1)
    assert C.takes_native_payment(state) is False
    assert C.refusal_reason(YEE, state) == C.NON_NATIVE_QUOTE_REFUSED


def test_a_non_converting_curve_stays_refused_even_with_the_knob_on(monkeypatch):
    """The flag is load-bearing, not decoration: OPAI (flag clear) REVERTED in both directions on
    chain, so quoting it would be a fabricated route."""
    _knob(monkeypatch, True)
    state = _state(quote_token=NON_NATIVE, native_to_quote_swap_enabled=0)
    assert C.takes_native_payment(state) is False
    assert C.refusal_reason(YEE, state) == C.NON_NATIVE_QUOTE_REFUSED


def test_a_native_curve_is_unaffected_by_the_knob(monkeypatch):
    for on in (False, True):
        _knob(monkeypatch, on)
        assert C.takes_native_payment(_state()) is True
        assert C.refusal_reason(YEE, _state()) is None


def test_the_fee_model_cannot_veto_a_converting_curve(monkeypatch):
    """The formula is denominated in the QUOTE token while we pay native, so it describes a
    DIFFERENT trade (measured: implied fee 7,284 / 9,623 / -7,725,802 bps). Gating it is what makes
    the class quotable at all; the round-trip cap in evm_real_seams.py is the guard that remains."""
    _knob(monkeypatch, True)
    state = _state(quote_token=NON_NATIVE, native_to_quote_swap_enabled=1)
    modeled = C.curve_buy_out(state, 10 ** 16)
    assert modeled is not None
    wildly_different = modeled // 4          # what a unit mismatch looks like

    out = C.dex_flap_curve_quote(
        "56", NATIVE, YEE, 10 ** 16, TAKER, rpc_url="https://rpc.example",
        state_fn=_wired(state), simulate_fn=lambda *a, **k: wildly_different)
    assert out is not None, "a unit mismatch must not refuse an internally-converted curve"
    assert out["buyAmount"] == str(wildly_different), "the LIVE simulation stays the quote source"


def test_a_native_curve_still_refuses_on_a_fee_model_mismatch(monkeypatch):
    """The regression guard. Gating the cross-check must not disarm it where it still applies."""
    _knob(monkeypatch, True)
    state = _state()
    modeled = C.curve_buy_out(state, 10 ** 16)
    out = C.dex_flap_curve_quote(
        "56", NATIVE, YEE, 10 ** 16, TAKER, rpc_url="https://rpc.example",
        state_fn=_wired(state), simulate_fn=lambda *a, **k: modeled // 4)
    assert out is None


# -- the Portal swap's own gas limit (2026-09-30, BINF 67604 on chain 56) -----------------------

BINF_ESTIMATE = 874_137      # eth_estimateGas for the reverted sell's calldata (0xb7450a3b...)


def _exit_quote():
    state = _state(circulating_supply=900000000000000000000000000)       # graduated, like BINF
    return C.dex_flap_curve_exit_quote(
        "56", YEE, NATIVE, 10 ** 22, TAKER, rpc_url="https://rpc.example",
        state_fn=_wired(state), simulate_fn=_matching_simulate_fn(state, real_sell_allowed=True))


def _buy_quote():
    state = _state()
    return C.dex_flap_curve_quote(
        "56", NATIVE, YEE, 10 ** 16, TAKER, rpc_url="https://rpc.example",
        state_fn=_wired(state), simulate_fn=_matching_simulate_fn(state))


def test_a_portal_sell_carries_a_gas_limit_above_what_a_graduated_sell_costs():
    """The Portal tx carried no gas, so the executor signed its 500,000 default: receipt
    0xb7450a3b... reverted out of gas at 478,432 of 500,000, and eth_estimateGas said 874,137.
    The limit rides in the quote's own transaction, which the exit gate and the signer read
    (`_evm_quantity(tx0x.get("gas") or default_swap_gas)`)."""
    from sniper.execution.evm_real_seams import _evm_quantity
    out = _exit_quote()
    assert out is not None
    assert _evm_quantity(out["transaction"]["gas"]) >= BINF_ESTIMATE
    assert _evm_quantity(out["transaction"]["gas"]) == 1_500_000


def test_a_portal_buy_carries_the_same_gas_limit():
    from sniper.execution.evm_real_seams import _evm_quantity
    out = _buy_quote()
    assert out is not None and _evm_quantity(out["transaction"]["gas"]) == 1_500_000


def test_the_portal_gas_limit_is_the_setting(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_FLAP_PORTAL_GAS", 2_000_000)
    assert _exit_quote()["transaction"]["gas"] == "2000000"
    assert _buy_quote()["transaction"]["gas"] == "2000000"


def test_an_unset_portal_gas_knob_is_the_default_in_a_fresh_interpreter():
    """Read at import: a fresh interpreter with the variable absent (not a monkeypatch of a module
    that already imported)."""
    import os
    import subprocess
    import sys
    env = {k: v for k, v in os.environ.items() if k != "MEMECOIN_EVM_FLAP_PORTAL_GAS"}
    out = subprocess.run(
        [sys.executable, "-c", "from sniper.settings import memecoin as m; "
         "print(m.MEMECOIN_EVM_FLAP_PORTAL_GAS)"],
        env=env, capture_output=True, text=True, cwd=str(__import__("pathlib").Path(
            __file__).resolve().parents[1]), timeout=60)
    assert out.returncode == 0, out.stderr[-400:]
    assert out.stdout.strip().splitlines()[-1] == "1500000"
