"""The atomic PCS V3 -> V2 mixed hop: one transaction, and an exit proved before any entry.

The encoding is checked by DECODING IT BACK with eth_abi, not by comparing to a hand-written hex
string. A hand-written expectation only proves the test author and the encoder made the same
mistake, and this calldata spends real money.

Selectors were verified in the deployed SmartRouter bytecode on 2026-09-22:
multicall(uint256,bytes[]) 0x5ae401dc, exactInputSingle(...) 0x04e45aaf,
swapExactTokensForTokens(...) 0x472b43f3, and on 2026-09-25 exactInput(...) 0xb858183f, which the
V3 leg now uses because the bridge is often two pools (WBNB -> USDT -> oANTHROPIC).

THE SECOND LEG SPENDS THE ROUTER'S WHOLE BALANCE (amountIn = 0). Verified 2026-09-25 by read-only
eth_call on chain 56 for 螃蟹 via oANTHROPIC: with the sentinel both the entry and the exit
multicall execute (round trip -2.28% at 0.1 WBNB); with an EXPLICIT amount on the second leg both
revert `STF`, because the router then pulls from msg.sender, who holds none of the bridged token.
The first version of this file asserted the explicit-amount design.
"""
import pytest
from eth_abi import decode

from sniper.execution import evm_mixed_hop_router as R


QUOTE = "0x" + "11" * 20          # WBNB / USDT side
BASE = "0x" + "bb" * 20           # the non-connector base token (BEM, MCDon, BNCB, oANTHROPIC)
COIN = "0x" + "cc" * 20
TAKER = "0x" + "dd" * 20
ROUTER = R.SMART_ROUTERS["56"]
FEE = 2500
DEADLINE = 1790000000


def _split_multicall(calldata):
    """(deadline, [leg calldata, ...]) decoded from multicall(uint256,bytes[])."""
    assert calldata.startswith("0x" + R.MULTICALL_DEADLINE), "wrong multicall selector"
    body = bytes.fromhex(calldata[2 + 8:])
    deadline, legs = decode(["uint256", "bytes[]"], body)
    return deadline, ["0x" + leg.hex() for leg in legs]


def _decode_v2(leg):
    assert leg.startswith("0x" + R.SWAP_EXACT_TOKENS_FOR_TOKENS)
    return decode(["uint256", "uint256", "address[]", "address"],
                  bytes.fromhex(leg[2 + 8:]))


def _decode_v3(leg):
    """(tokens, fees, recipient, amount_in, amount_out_min) of an exactInput leg."""
    from sniper.execution.evm_dex_quote import EXACT_INPUT
    assert leg.startswith("0x" + EXACT_INPUT), "the SmartRouter takes the no-deadline path form"
    (path, recipient, amt_in, amt_min), = decode(["(bytes,address,uint256,uint256)"],
                                                 bytes.fromhex(leg[2 + 8:]))
    tokens, fees = ["0x" + path[:20].hex()], []
    rest = path[20:]
    while rest:
        fees.append(int.from_bytes(rest[:3], "big"))
        tokens.append("0x" + rest[3:23].hex())
        rest = rest[23:]
    return tokens, fees, recipient, amt_in, amt_min


# --- the router itself ----------------------------------------------------------------------------

def test_only_a_verified_chain_has_a_router():
    assert R.smart_router_for("56") == ROUTER
    assert R.smart_router_for(56) == ROUTER
    assert R.smart_router_for("8453") is None, "never guess a router for an unverified chain"
    assert R.smart_router_for("solana") is None


# --- entry ------------------------------------------------------------------------------------------

def test_the_entry_is_one_transaction_with_both_legs_in_order():
    data = R.build_entry_calldata(
        quote_token=QUOTE, base_token=BASE, coin=COIN, v3_fee=FEE, amount_in=1_000_000,
        bridged_min=500, coin_min=9_000, recipient=TAKER, deadline=DEADLINE, router=ROUTER)
    deadline, legs = _split_multicall(data)
    assert deadline == DEADLINE, "the deadline rides on the multicall, not the V3 leg"
    assert len(legs) == 2, "two legs, one transaction"

    tokens, fees, recipient, amt_in, amt_min = _decode_v3(legs[0])
    assert tokens == [QUOTE, BASE], "leg 1 bridges quote -> base on V3"
    assert fees == [FEE]
    assert recipient.lower() == ROUTER, "leg 1 must pay the ROUTER so leg 2 can spend it"
    assert (amt_in, amt_min) == (1_000_000, 500)

    v2_in, v2_min, path, to = _decode_v2(legs[1])
    assert [p.lower() for p in path] == [BASE, COIN], "leg 2 buys the coin on V2"
    assert to.lower() == TAKER, "leg 2 must pay US, not the router"
    assert v2_in == R.CONTRACT_BALANCE == 0, "leg 2 spends the router's whole balance"
    assert v2_min == 9_000


def test_leg_two_spends_the_routers_whole_balance_never_an_explicit_amount():
    """An explicit amount on leg 2 is paid from msg.sender and reverts `STF` (verified on chain,
    2026-09-25). The sentinel spends whatever leg 1 delivered, floored by leg 1's own minimum, so
    nothing is left behind in the router either."""
    data = R.build_entry_calldata(
        quote_token=QUOTE, base_token=BASE, coin=COIN, v3_fee=FEE, amount_in=1_000_000,
        bridged_min=777, coin_min=1, recipient=TAKER, deadline=DEADLINE, router=ROUTER)
    _deadline, legs = _split_multicall(data)
    assert _decode_v3(legs[0])[4] == 777        # leg 1 amountOutMinimum still floors the bridge
    assert _decode_v2(legs[1])[0] == 0          # leg 2 amountIn = CONTRACT_BALANCE


def test_a_two_pool_bridge_encodes_the_whole_path():
    """WBNB has no pool with oANTHROPIC; the bridge is WBNB -(100)-> USDT -(2500)-> oANTHROPIC."""
    usdt = "0x" + "55" * 20
    data = R.build_entry_calldata(
        quote_token=QUOTE, base_token=BASE, coin=COIN, v3_tokens=[QUOTE, usdt, BASE],
        v3_fees=[100, 2500], amount_in=10, bridged_min=5, coin_min=1, recipient=TAKER,
        deadline=DEADLINE, router=ROUTER)
    _deadline, legs = _split_multicall(data)
    tokens, fees, *_rest = _decode_v3(legs[0])
    assert tokens == [QUOTE, usdt, BASE] and fees == [100, 2500]
    with pytest.raises(ValueError):
        R.build_entry_calldata(
            quote_token=QUOTE, base_token=BASE, coin=COIN, v3_tokens=[QUOTE, usdt],
            v3_fees=[100], amount_in=10, bridged_min=5, coin_min=1, recipient=TAKER,
            deadline=DEADLINE, router=ROUTER)


# --- exit -------------------------------------------------------------------------------------------

def test_the_exit_is_the_mirror_through_the_same_router():
    data = R.build_exit_calldata(
        coin=COIN, base_token=BASE, quote_token=QUOTE, v3_fee=FEE, amount_in=42_000,
        bridged_min=300, quote_min=100, recipient=TAKER, deadline=DEADLINE, router=ROUTER)
    deadline, legs = _split_multicall(data)
    assert deadline == DEADLINE and len(legs) == 2

    v2_in, v2_min, path, to = _decode_v2(legs[0])
    assert [p.lower() for p in path] == [COIN, BASE], "leg 1 sells the coin on V2"
    assert to.lower() == ROUTER, "leg 1 pays the router so the V3 leg can spend it"
    assert (v2_in, v2_min) == (42_000, 300)

    tokens, _fees, recipient, amt_in, amt_min = _decode_v3(legs[1])
    assert tokens == [BASE, QUOTE], "leg 2 bridges back on V3"
    assert recipient.lower() == TAKER and amt_min == 100
    assert amt_in == R.CONTRACT_BALANCE, "leg 2 sells the router's whole balance"


# --- the pre-entry exit check ------------------------------------------------------------------------

def _sim(status, detail=None, **kw):
    seen = {}

    def call_fn(tx, overrides):
        seen["tx"] = tx
        return status, None, detail
    ok, reason = R.simulate_exit(
        chain="56", coin=COIN, base_token=BASE, quote_token=QUOTE, v3_fee=FEE,
        amount_in=42_000, taker=TAKER, rpc_url="http://rpc.invalid", deadline=DEADLINE,
        call_fn=call_fn, **kw)
    return ok, reason, seen


def test_a_simulated_exit_that_executes_admits_the_path():
    ok, reason, seen = _sim(True)
    assert ok is True and reason == "exit_simulated"
    assert seen["tx"]["to"] == ROUTER
    assert seen["tx"]["data"].startswith("0x" + R.MULTICALL_DEADLINE)


def test_a_reverting_exit_refuses_and_carries_the_reason():
    ok, reason, _ = _sim(False, detail="TRANSFER_FROM_FAILED")
    assert ok is False
    assert reason.startswith("exit_sim_reverted:") and "TRANSFER_FROM_FAILED" in reason


def test_transport_failure_is_not_an_unsellable_coin():
    """A 429 must not read as a honeypot. It refuses — fail closed — but under its own name, so the
    two are countable apart."""
    ok, reason, _ = _sim(None)
    assert ok is False and reason == "exit_sim_unknown"


def test_the_simulation_asks_whether_the_path_executes_not_whether_it_is_cheap():
    """The minimums default to 1 wei. Pricing is the quote's job; refusing a sellable coin for
    being cheap would strand it for the wrong reason."""
    _ok, _reason, seen = _sim(True)
    _deadline, legs = _split_multicall(seen["tx"]["data"])
    assert _decode_v2(legs[0])[1] == 1, "leg 1 minimum is a liveness floor, not a price"
    assert _decode_v3(legs[1])[4] == 1


def test_an_unverified_chain_cannot_be_simulated():
    ok, reason = R.simulate_exit(
        chain="8453", coin=COIN, base_token=BASE, quote_token=QUOTE, v3_fee=FEE,
        amount_in=1, taker=TAKER, rpc_url="http://rpc.invalid", deadline=DEADLINE,
        call_fn=lambda tx, o: (True, None, None))
    assert ok is False and reason == "no_verified_smart_router"


def test_a_raising_call_never_raises_out_of_the_check():
    def boom(tx, overrides):
        raise RuntimeError("rpc down")
    ok, reason = R.simulate_exit(
        chain="56", coin=COIN, base_token=BASE, quote_token=QUOTE, v3_fee=FEE,
        amount_in=1, taker=TAKER, rpc_url="http://rpc.invalid", deadline=DEADLINE, call_fn=boom)
    assert ok is False and reason == "exit_sim_error"


# --- encoding invariants ------------------------------------------------------------------------------

def test_the_default_simulation_refuses_instead_of_raising_a_symbol_error():
    """E7, 2026-09-23. With no `call_fn` injected, this function imported `_fund_overrides` from
    `evm_token_safety` as a module symbol. It is a CLOSURE inside `build_evm_token_safety`, over
    `taker`, `rpc_url`, `http` and `_alternates`, so the import always raised -- and it sits
    outside the `try` below, so the ImportError left a function whose whole contract is
    `(ok, reason)`. Every test injected the seam, so the default construction path never ran.

    Failing closed with a name is the honest state: this module has no production caller, and
    without an overrides builder it cannot simulate an exit at all."""
    ok, reason = R.simulate_exit(
        chain="56", coin=COIN, base_token=BASE, quote_token=QUOTE, v3_fee=FEE,
        amount_in=42_000, taker=TAKER, rpc_url="http://rpc.invalid", deadline=DEADLINE)
    assert ok is False
    assert reason == "exit_overrides_unsupported", reason


def test_the_default_planner_yields_no_plan_and_raises_nothing():
    """The whole point of the finding: `plan_mixed_entry(enabled=True)` on its default seam threw
    before any network request."""
    plan, reason = R.plan_mixed_entry(
        chain="56", quote_token=QUOTE, base_token=BASE, coin=COIN, v3_fee=FEE,
        amount_in=1000, bridged_min=100, coin_min=90, taker=TAKER,
        rpc_url="http://rpc.invalid", deadline=DEADLINE, expected_coin_out=100, enabled=True)
    assert plan is None
    assert reason == "exit_unproven:exit_overrides_unsupported", reason


def test_an_injected_overrides_builder_reaches_the_call():
    """The parameter is not decoration: what it returns is what the simulation overrides with, and
    a builder that finds no layout still refuses under its own existing name."""
    seen = {}

    def fund_overrides_fn(mint, spender, amount):
        seen["args"] = (mint, spender, amount)
        return {mint: {"stateDiff": {"0x01": "0xff"}}}

    def call_fn(tx, overrides):
        seen["overrides"] = overrides
        return True, None, None

    ok, reason = R.simulate_exit(
        chain="56", coin=COIN, base_token=BASE, quote_token=QUOTE, v3_fee=FEE,
        amount_in=42_000, taker=TAKER, rpc_url="http://rpc.invalid", deadline=DEADLINE,
        fund_overrides_fn=fund_overrides_fn, call_fn=call_fn)
    assert ok is True and reason == "exit_simulated"
    assert seen["args"] == (COIN, ROUTER, 42_000), seen
    assert seen["overrides"] == {COIN: {"stateDiff": {"0x01": "0xff"}}}, seen


def test_an_undiscoverable_layout_keeps_its_own_name():
    """Three different facts, three strings: we never had a builder
    (`exit_overrides_unsupported`), the builder raised (`exit_overrides_unavailable`), and the
    builder answered that the token's slot layout was not found
    (`exit_overrides_undiscoverable`)."""
    ok, reason = R.simulate_exit(
        chain="56", coin=COIN, base_token=BASE, quote_token=QUOTE, v3_fee=FEE,
        amount_in=42_000, taker=TAKER, rpc_url="http://rpc.invalid", deadline=DEADLINE,
        fund_overrides_fn=lambda *_a: None, call_fn=lambda _tx, _o: (True, None, None))
    assert ok is False and reason == "exit_overrides_undiscoverable", reason

    def boom(*_a):
        raise RuntimeError("slot probe died")
    ok, reason = R.simulate_exit(
        chain="56", coin=COIN, base_token=BASE, quote_token=QUOTE, v3_fee=FEE,
        amount_in=42_000, taker=TAKER, rpc_url="http://rpc.invalid", deadline=DEADLINE,
        fund_overrides_fn=boom, call_fn=lambda _tx, _o: (True, None, None))
    assert ok is False and reason == "exit_overrides_unavailable", reason


def test_a_v2_leg_needs_a_real_path():
    with pytest.raises(ValueError):
        R.encode_v2_leg(1, 1, [COIN], TAKER)


def test_a_multicall_needs_a_call():
    with pytest.raises(ValueError):
        R.encode_multicall(DEADLINE, [])


def test_legs_of_unequal_length_still_decode():
    """The bytes[] tail is offset-addressed, so a short leg beside a long one must not shift it."""
    short = R.encode_v2_leg(1, 2, [BASE, COIN], TAKER)
    long = R.encode_v2_leg(3, 4, [BASE, COIN, QUOTE, TAKER], ROUTER)
    _deadline, legs = _split_multicall(R.encode_multicall(DEADLINE, [short, long, short]))
    assert len(legs) == 3
    assert _decode_v2(legs[1])[0] == 3
    assert len(_decode_v2(legs[1])[2]) == 4
    assert _decode_v2(legs[2])[0] == 1


# --- the plan: no proven exit, no entry ---------------------------------------------------------

def _plan(exit_ok=True, exit_reason="exit_simulated", enabled=True, expected_coin_out=9_000,
          **kw):
    seen = {}

    def exit_sim_fn(**call):
        seen.update(call)
        return exit_ok, exit_reason
    plan, reason = R.plan_mixed_entry(
        chain="56", quote_token=QUOTE, base_token=BASE, coin=COIN, v3_fee=FEE,
        amount_in=1_000_000, bridged_min=500, coin_min=8_500, taker=TAKER,
        rpc_url="http://rpc.invalid", deadline=DEADLINE,
        expected_coin_out=expected_coin_out, exit_sim_fn=exit_sim_fn, enabled=enabled, **kw)
    return plan, reason, seen


def test_a_plan_exists_only_when_the_exit_simulated():
    plan, reason, _ = _plan()
    assert reason == "planned" and plan["exit_proven"] is True
    assert plan["to"] == ROUTER and plan["data"].startswith("0x" + R.MULTICALL_DEADLINE)


def test_an_unproven_exit_yields_NO_PLAN_not_a_warning():
    """THE SAFETY PROPERTY. A caller cannot forget to check the exit, because without one there is
    nothing to sign. These coins have no single-venue fallback by construction."""
    plan, reason, _ = _plan(exit_ok=False, exit_reason="exit_sim_reverted:TRANSFER_FROM_FAILED")
    assert plan is None
    assert reason.startswith("exit_unproven:exit_sim_reverted")


def test_a_transport_failure_also_yields_no_plan():
    """Fail closed: "we could not ask" is not "we may sell"."""
    plan, reason, _ = _plan(exit_ok=False, exit_reason="exit_sim_unknown")
    assert plan is None and reason == "exit_unproven:exit_sim_unknown"


def test_the_exit_is_simulated_at_the_WHOLE_would_be_balance():
    """A probe-sized simulation proves we can sell SOME of it, which is not the question."""
    _plan_out, _reason, seen = _plan(expected_coin_out=123_456)
    assert seen["amount_in"] == 123_456
    assert seen["coin"] == COIN and seen["chain"] == "56"


def test_the_plan_names_the_new_spender_to_approve():
    """The SmartRouter is not the router the single-venue paths already hold an allowance for."""
    plan, _reason, _ = _plan()
    assert plan["approve_spender"] == ROUTER
    assert plan["approve_token"] == QUOTE
    assert plan["approve_amount"] == 1_000_000


def test_it_is_off_by_default():
    called = []

    def exit_sim_fn(**_kw):
        called.append(1)
        return True, "exit_simulated"
    plan, reason = R.plan_mixed_entry(
        chain="56", quote_token=QUOTE, base_token=BASE, coin=COIN, v3_fee=FEE, amount_in=1,
        bridged_min=1, coin_min=1, taker=TAKER, rpc_url="http://rpc.invalid", deadline=DEADLINE,
        expected_coin_out=1, exit_sim_fn=exit_sim_fn, enabled=False)
    assert plan is None and reason == "mixed_hop_execution_disabled"
    assert called == [], "a disabled path still simulated an exit"


def test_no_expected_output_is_no_plan():
    plan, reason, _ = _plan(expected_coin_out=0)
    assert plan is None and reason == "no_expected_output"
