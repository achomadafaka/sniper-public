"""B's transaction builder: one atomic V4 multi-hop swap (spec Amendment 5, gate item 1).

Alpha's requirements, each pinned here:
  1. ATOMIC — one UniversalRouter execute, all legs. A mid-route failure would strand us in the
     bridge currency, which is not our bankroll and not a coin we chose: an unexitable-bag shape.
  2. MIN-OUT ON THE FINAL LEG ONLY. Per-leg minimums can each pass while the compounded
     end-to-end result is bad. The invariant that matters is coin-out for cash-in.
  3. THE EXIT IS BUILT IN THIS SAME COMMIT. A four-leg entry with an unbuilt two-leg exit is the
     largest possible version of "can buy where we cannot sell".
  4. It must present to the premium gate as cash-in / coin-out for the whole clip, with the
     bridge currency never appearing.
"""
import pytest

import sniper.execution.evm_v4_multihop_tx as TX

CHAIN = "4663"
NATIVE = "0x" + "00" * 20
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
COIN = "0xffbb" + "22" * 18
HOOKS = "0x" + "00" * 20


def _leg(c0, c1, fee=460, ts=60):
    return {"currency0": c0, "currency1": c1, "fee": fee, "tick_spacing": ts, "hooks": HOOKS}


ROUTE = {
    "bridge": USDG,
    "bridge_key": _leg(NATIVE, USDG, fee=460),
    "coin_key": _leg(USDG, COIN, fee=3000),
    "coin_pool_id": "p_coin", "bridge_pool_id": "p_bridge",
}


@pytest.fixture(autouse=True)
def _stub_permit(monkeypatch):
    """Permit2 signing is covered elsewhere; these tests assert the multi-hop SHAPE."""
    monkeypatch.setattr(TX, "_permit2_input", lambda *a, **k: "00" * 64)


def _words(hexstr):
    h = hexstr[2:] if hexstr.startswith("0x") else hexstr
    return [h[i:i + 64] for i in range(0, len(h), 64)]


# --- requirement 1: atomicity ----------------------------------------------------------------

def test_entry_is_exactly_one_v4_swap_command():
    """Two V4_SWAP commands would be two settlements and a window where we hold USDG."""
    r = TX.build_multihop_entry(CHAIN, NATIVE, COIN, ROUTE, 10**16, 500, taker="0x" + "11" * 20)
    assert r is not None
    assert r["commands"].count(bytes([TX._CMD_V4_SWAP])) == 1
    assert len(r["commands"]) == r["commands"].count(bytes([TX._CMD_V4_SWAP])) * 1 + \
        (1 if TX._CMD_PERMIT2_PERMIT in r["commands"] else 0)


def test_the_swap_uses_the_MULTIHOP_action_not_the_single_one():
    r = TX.build_multihop_entry(CHAIN, NATIVE, COIN, ROUTE, 10**16, 500, taker="0x" + "11" * 20)
    assert TX._ACT_SWAP_EXACT_IN in r["actions"], "used the single-pool action for a two-hop route"
    assert TX._ACT_SWAP_IN_SINGLE not in r["actions"]


# --- requirement 2: one end-to-end minimum ---------------------------------------------------

def test_min_out_appears_once_and_is_the_final_leg_amount():
    """A per-leg minimum can pass every hop and still deliver a bad end-to-end result."""
    MIN = 0xABCDEF
    r = TX.build_multihop_entry(CHAIN, NATIVE, COIN, ROUTE, 10**16, MIN, taker="0x" + "11" * 20)
    hits = [w for w in _words(r["swap_input"]) if int(w, 16) == MIN]
    assert len(hits) == 2, (
        "expected min_out exactly twice (ExactInputParams.amountOutMinimum and TAKE_ALL), got %d"
        % len(hits))


def test_no_intermediate_minimum_is_encoded():
    """The path carries fees and hooks, never per-hop minimums."""
    r = TX.build_multihop_entry(CHAIN, NATIVE, COIN, ROUTE, 10**16, 500, taker="0x" + "11" * 20)
    assert r["path_len"] == 2
    assert r["per_leg_minimums"] is False


# --- requirement 3: the exit, built here ------------------------------------------------------

def test_exit_is_built_and_reverses_the_path():
    r = TX.build_multihop_exit(CHAIN, COIN, NATIVE, ROUTE, 10**18, 400,
                               taker="0x" + "11" * 20, acct=object())
    assert r is not None
    assert r["currency_in"].lower() == COIN.lower()
    assert [c.lower() for c in r["path_currencies"]] == [USDG.lower(), NATIVE.lower()]


def test_exit_is_also_a_single_atomic_swap():
    r = TX.build_multihop_exit(CHAIN, COIN, NATIVE, ROUTE, 10**18, 400,
                               taker="0x" + "11" * 20, acct=object())
    assert r["commands"].count(bytes([TX._CMD_V4_SWAP])) == 1


def test_native_exit_keeps_output_in_the_router_then_wraps_to_weth():
    """The funded balance reader sees WETH. Native output must not stay as gas after an exit."""
    r = TX.build_multihop_exit(CHAIN, COIN, NATIVE, ROUTE, 10**18, 400,
                               taker="0x" + "11" * 20, acct=object())
    assert TX._ACT_TAKE in r["actions"]
    assert TX._ACT_TAKE_ALL not in r["actions"]
    assert TX._CMD_WRAP_ETH in r["commands"]


def test_exit_requires_a_signer():
    """Selling an ERC-20 needs a permit2 signature; no signer must refuse, never fall back."""
    assert TX.build_multihop_exit(CHAIN, COIN, NATIVE, ROUTE, 10**18, 400,
                                  taker="0x" + "11" * 20, acct=None) is None


# --- entry path currencies --------------------------------------------------------------------

def test_entry_path_is_bridge_then_coin():
    r = TX.build_multihop_entry(CHAIN, NATIVE, COIN, ROUTE, 10**16, 500, taker="0x" + "11" * 20)
    assert r["currency_in"].lower() == NATIVE.lower()
    assert [c.lower() for c in r["path_currencies"]] == [USDG.lower(), COIN.lower()]


def test_native_entry_sends_value_and_unwraps():
    r = TX.build_multihop_entry(CHAIN, NATIVE, COIN, ROUTE, 10**16, 500, taker="0x" + "11" * 20)
    assert r["value"] == hex(10**16)
    assert r["pre_transactions"], "native entry must unwrap WETH first"
    from sniper.services.memecoin_evm_gas_topup import WETH_CALL_GAS_LIMIT
    assert r["pre_transactions"][0]["gas"] == str(WETH_CALL_GAS_LIMIT)


# --- requirement 4: the premium gate can price it ----------------------------------------------

def test_quote_presents_as_cash_in_coin_out_with_no_bridge():
    """The premium fields contain only the cash input and the coin output."""
    q = TX.as_zerox_quote(CHAIN, NATIVE, COIN, ROUTE, 10**16, 500, out_amount=7000,
                          taker="0x" + "11" * 20)
    assert q["sellAmount"] == str(10**16)
    assert q["buyAmount"] == "7000"
    assert "bridgeAmount" not in q


def test_route_records_that_it_is_multihop_for_evidence():
    q = TX.as_zerox_quote(CHAIN, NATIVE, COIN, ROUTE, 10**16, 500, out_amount=7000,
                          taker="0x" + "11" * 20)
    assert q["route"]["legs"] == 2
    assert q["route"]["protocol"] == "uniswap_v4_multihop"
    assert q["route"]["bridge"] == USDG
    assert q["route"]["bridge_key"] == ROUTE["bridge_key"]
    assert q["route"]["coin_key"] == ROUTE["coin_key"]


# --- fail closed ------------------------------------------------------------------------------

def test_refuses_when_the_route_is_incomplete():
    for missing in ("bridge_key", "coin_key", "bridge"):
        bad = {k: v for k, v in ROUTE.items() if k != missing}
        assert TX.build_multihop_entry(CHAIN, NATIVE, COIN, bad, 10**16, 500,
                                       taker="0x" + "11" * 20) is None


def test_refuses_amounts_that_do_not_fit_uint128():
    assert TX.build_multihop_entry(CHAIN, NATIVE, COIN, ROUTE, 1 << 128, 500,
                                   taker="0x" + "11" * 20) is None


# --- requirement 5: the fee screen applies to EVERY leg ---------------------------------------

@pytest.mark.parametrize("bad_leg", ["bridge_key", "coin_key"])
@pytest.mark.parametrize("bad_fee", [864114, 900000, None])
def test_a_trap_fee_on_ANY_leg_refuses_the_route(bad_leg, bad_fee):
    """A two-hop route touching one trap leg is a 99% loss wearing a routable label. The decision
    layer screens too; this is the second line, because a route arriving here with an unsane fee
    is a wiring bug and must not be built into a transaction."""
    route = dict(ROUTE)
    route[bad_leg] = dict(route[bad_leg], fee=bad_fee)
    assert TX.build_multihop_entry(CHAIN, NATIVE, COIN, route, 10**16, 500,
                                   taker="0x" + "11" * 20) is None
    assert TX.build_multihop_exit(CHAIN, COIN, NATIVE, route, 10**18, 400,
                                  taker="0x" + "11" * 20, acct=object()) is None


def test_a_sane_fee_on_every_leg_is_built():
    """The control: the screen must not refuse a good route."""
    assert TX.build_multihop_entry(CHAIN, NATIVE, COIN, ROUTE, 10**16, 500,
                                   taker="0x" + "11" * 20) is not None


# --- ABI STRUCTURE, decoded back from the calldata we actually emit ---------------------------
# A V4 encoding error REVERTS rather than raising, and the router gives EMPTY revert data, so
# there is nothing to read at the node. These decode the emitted bytes instead of trusting the
# builder's own view of them.
#
# The first version of this encoder omitted `minHopPriceX36` and every multi-hop entry would have
# reverted. It was caught by read-only eth_call simulation, NOT by tests: 23 tests passed while
# the encoding was wrong, because they proved the encoder agreed with the decoder and both were
# wrong about the router.

def _decode_swap_input(si):
    W = [si[i:i + 64] for i in range(0, len(si), 64)]
    out = {"actions": [int(W[3][i:i + 2], 16) for i in range(0, int(W[2], 16) * 2, 2)]}
    pbase = int(W[1], 16) // 32
    out["n_params"] = int(W[pbase], 16)
    b = pbase + 1 + int(W[pbase + 1], 16) // 32 + 1
    out["struct_offset"] = int(W[b], 16)
    out["currency_in"] = "0x" + W[b + 1][24:]
    # FIVE head fields: currencyIn, offset(path), offset(minHopPriceX36), amountIn, minOut.
    out["min_hop_len"] = int(W[b + 1 + int(W[b + 3], 16) // 32], 16)
    out["amount_in"] = int(W[b + 4], 16)
    out["min_out"] = int(W[b + 5], 16)
    pb = b + 1 + int(W[b + 2], 16) // 32
    out["path"] = []
    for i in range(int(W[pb], 16)):
        k = pb + 1 + int(W[pb + 1 + i], 16) // 32
        out["path"].append({"currency": "0x" + W[k][24:], "fee": int(W[k + 1], 16),
                            "tick_spacing": int(W[k + 2], 16), "hook_data_len": int(W[k + 5], 16)})
    return out


def test_encoded_calldata_decodes_to_the_route_we_intended():
    AMT, MIN = 10 ** 16, 0xABCDEF
    r = TX.build_multihop_entry(CHAIN, NATIVE, COIN, ROUTE, AMT, MIN, taker="0x" + "11" * 20)
    d = _decode_swap_input(r["swap_input"])
    assert d["actions"] == [TX._ACT_SWAP_EXACT_IN, TX._ACT_SETTLE_ALL, TX._ACT_TAKE_ALL]
    assert d["struct_offset"] == 0x20, "missing leading struct offset -> every swap reverts"
    assert d["currency_in"].lower() == NATIVE
    assert d["amount_in"] == AMT
    assert d["min_out"] == MIN
    assert [p["currency"].lower() for p in d["path"]] == [USDG.lower(), COIN.lower()]
    assert [p["fee"] for p in d["path"]] == [460, 3000], "per-hop fees in the wrong order"


def test_minHopPriceX36_is_present_and_empty():
    """The omission that made every entry revert. Present, length zero: no per-hop price bound,
    so the end-to-end amountOutMinimum stays the only bound."""
    r = TX.build_multihop_entry(CHAIN, NATIVE, COIN, ROUTE, 10 ** 16, 500, taker="0x" + "11" * 20)
    assert _decode_swap_input(r["swap_input"])["min_hop_len"] == 0


def test_the_encoded_minimum_bounds_the_FINAL_leg_only():
    MIN = 0x123456
    r = TX.build_multihop_entry(CHAIN, NATIVE, COIN, ROUTE, 10 ** 16, MIN, taker="0x" + "11" * 20)
    d = _decode_swap_input(r["swap_input"])
    assert d["min_out"] == MIN
    for hop in d["path"]:
        assert MIN not in hop.values(), "a per-hop minimum leaked into the path"


def test_exit_calldata_decodes_to_the_reversed_route():
    r = TX.build_multihop_exit(CHAIN, COIN, NATIVE, ROUTE, 10 ** 18, 400,
                               taker="0x" + "11" * 20, acct=object())
    d = _decode_swap_input(r["swap_input"])
    assert d["currency_in"].lower() == COIN.lower()
    assert [p["currency"].lower() for p in d["path"]] == [USDG.lower(), NATIVE.lower()]
    assert [p["fee"] for p in d["path"]] == [3000, 460], "exit fees not reversed with the path"
    assert d["min_hop_len"] == 0
