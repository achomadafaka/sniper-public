"""Price mixed entries from the existing whole-balance exit call, not guessed exit taxes."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from eth_abi import decode, encode
from eth_utils import keccak

from sniper.execution import evm_mixed_hop_router as mixed, evm_real_seams as seams

QUOTE = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"
BASE = "0x94be0bba8e1e303fe998c9360b57b826f1a4f828"
COIN = "0xe3baec7817b97eb8cbc0541e510b5e6e944b7777"
TAKER = "0x" + "ab" * 20
CLIP = 10**18
CFG = SimpleNamespace(
    MEMECOIN_ENTRY_SLIPPAGE_PCT=3,
    MEMECOIN_EVM_ROUNDTRIP_TAX_ALLOWANCE_PCT=4,
    memecoin_evm_v2_exit_config=lambda chain: {"connectors": [QUOTE]},
)


def _result(amount, *, isolated=True):
    legs = [encode(["uint256"], [CLIP]), encode(["uint256"], [amount])]
    return "0x" + encode(["bytes[]"], [[b"", *legs] if isolated else legs]).hex()


def test_old_public_return_without_inventory_isolation_is_not_priced():
    fixture = json.loads((Path(__file__).parent / 'fixtures/memecoin/'
                         'bsc_aikol_mixed_exit_result.json').read_text())
    raw = fixture['raw_result']
    legs, = decode(['bytes[]'], bytes.fromhex(raw[2:]))
    actual, = decode(['uint256'], legs[1])
    assert actual == int(fixture['quote_out_atomic'])
    assert mixed._exit_proceeds(raw) is None


def test_captured_isolated_public_result_matches_independent_abi_decoder():
    fixture = json.loads((Path(__file__).parent / 'fixtures/memecoin/'
                         'bsc_aikol_isolated_exit_result.json').read_text())
    raw = fixture['raw_result']
    legs, = decode(['bytes[]'], bytes.fromhex(raw[2:]))
    assert legs[0] == b''
    actual, = decode(['uint256'], legs[2])
    assert mixed._exit_proceeds(raw) == actual == int(fixture['quote_out_atomic'])


def test_held_position_exit_does_not_sweep_public_router_inventory():
    data = mixed.build_exit_calldata(
        coin=COIN, base_token=BASE, quote_token=QUOTE, amount_in=CLIP,
        bridged_min=1, quote_min=1, recipient=TAKER, deadline=2**32,
        router=mixed.SMART_ROUTERS['56'], v3_fee=2500)
    _, legs = decode(['uint256', 'bytes[]'], bytes.fromhex(data[10:]))
    assert len(legs) == 2
    assert legs[0][:4] == keccak(text='swapExactTokensForTokens(uint256,uint256,address[],address)')[:4]


def _quote(monkeypatch, *, response=None):
    calls = []

    def v3(chain, sell, buy, amount, taker, **kw):
        calls.append(("v3", sell, buy, amount))
        out = amount
        return {"buyAmount": str(out), "minBuyAmount": str(out * 9700 // 10000),
                "route": {"protocol": "pancake_v3", "tokens": [sell, buy], "fees": [2500]}}

    def v2(chain, sell, buy, amount, taker, **kw):
        calls.append(("v2", sell, buy, amount))
        # Model the real V2 quote seam's input deduction; the old test stub ignored it.
        out = amount * (10000 - kw["tax_buffer_bps"]) // 10000
        return {"buyAmount": str(out), "minBuyAmount": str(out * 9700 // 10000),
                "route": {"tokens": [sell, buy]}}

    def rpc(url, method, params, **kw):
        calls.append((method, params))
        return ({"result": _result(CLIP * 94 // 100)} if response is None else
                response(params) if callable(response) else response)

    monkeypatch.setattr(seams, "_rpc", rpc)
    overrides = lambda mint, spender, amount, **kw: {
        mint: {"stateDiff": {"0x" + "01" * 32: hex(amount)}}}
    q, why = mixed.mixed_entry_quote(
        "56", QUOTE, COIN, CLIP, TAKER, rpc_url="https://rpc.invalid",
        v3_quote_fn=v3, v2_quote_fn=v2, cfg=CFG,
        pair_tokens_fn=lambda *_: [BASE], fund_overrides_fn=overrides)
    return q, why, calls


def test_existing_exit_proceeds_replace_assumed_reverse_taxes(monkeypatch):
    q, why, calls = _quote(monkeypatch)
    assert why is None
    assert q["route"]["round_trip_pct"] == pytest.approx(-6)
    assert q["route"]["round_trip_back_atomic"] == str(CLIP * 94 // 100)
    assert q["route"]["round_trip_measured_by"] == "whole_balance_exit_simulation"
    from sniper.execution.evm_v4_multihop import round_trip_within_budget
    assert round_trip_within_budget(q["route"]["round_trip_pct"], 25, chain="56")
    # One simulation, two forward quotes. No reverse quoter, new RPC, or retry.
    assert [c[0] for c in calls] == ["v3", "v2", "eth_call"]
    tx, _, overrides = calls[-1][1]
    _, legs = decode(["uint256", "bytes[]"], bytes.fromhex(tx["data"][10:]))
    assert int.from_bytes(legs[1][4:36], "big") == int(q["buyAmount"])
    assert COIN in overrides
    _, entry_legs = decode(['uint256', 'bytes[]'],
                           bytes.fromhex(q['transaction']['data'][10:]))
    assert len(entry_legs) == 2  # Inventory isolation never enters our signed trade.


def test_donated_router_base_cannot_make_an_expensive_coin_look_cheap(monkeypatch):
    def reply(params):
        _, legs = decode(['uint256', 'bytes[]'], bytes.fromhex(params[0]['data'][10:]))
        isolated = len(legs) == 3
        if isolated:
            assert legs[0][:4] == keccak(text='sweepToken(address,uint256,address)')[:4]
            token, minimum, recipient = decode(['address', 'uint256', 'address'], legs[0][4:])
            assert (token, minimum, recipient) == (BASE, 0, TAKER)
        # The token sale earns 80%. Stray public-router inventory contributes another 18%
        # unless the exact same simulation clears it before the reverse swaps.
        back = CLIP * (80 if isolated else 98) // 100
        return {'result': _result(back, isolated=isolated)}

    q, why, calls = _quote(monkeypatch, response=reply)
    assert why is None
    assert q['route']['round_trip_pct'] == pytest.approx(-20)
    from sniper.execution.evm_v4_multihop import round_trip_within_budget
    assert not round_trip_within_budget(q['route']['round_trip_pct'], 25, chain='56')
    assert [c[0] for c in calls] == ['v3', 'v2', 'eth_call']


@pytest.mark.parametrize("raw", [None, "0x", "0xzz", _result(0),
    _result(CLIP, isolated=False),
    _result(CLIP)[:-2], _result(CLIP) + "00" * 32,
    "0x" + encode(["bytes[]"], [[encode(["uint256"], [CLIP])]]).hex(),
    "0x" + encode(["bytes[]"], [[b"", encode(["uint256"], [CLIP])]]).hex(),
    "0x" + encode(["bytes[]"], [[encode(["uint256"], [CLIP]),
        encode(["uint256"], [CLIP]), encode(["uint256"], [CLIP])]]).hex(),
])
def test_success_without_valid_priced_proceeds_refuses(monkeypatch, raw):
    q, why, _ = _quote(monkeypatch, response={"result": raw})
    assert q is None
    assert why == "round_trip_unmeasured:mixed_v3_v2"


def test_actual_expensive_proceeds_keep_the_budget_refusal(monkeypatch):
    q, why, _ = _quote(monkeypatch, response={"result": _result(CLIP // 2)})
    assert why is None
    from sniper.execution.evm_v4_multihop import round_trip_within_budget
    assert not round_trip_within_budget(q["route"]["round_trip_pct"], 25, chain="56")


@pytest.mark.parametrize("response,prefix", [
    ({"error": {"code": -32000, "message": "execution reverted: STF"}}, "exit_unproven:exit_sim_reverted"),
    ({"error": {"code": -32005, "message": "rate limit"}}, "exit_unproven:exit_sim_unknown"),
    ({}, "exit_unproven:exit_sim_unknown"),
])
def test_failed_price_simulation_cannot_admit(monkeypatch, response, prefix):
    q, why, _ = _quote(monkeypatch, response=response)
    assert q is None and why.startswith(prefix)


def test_raw_simulation_capture_is_per_call_and_cleared_on_unknown(monkeypatch):
    def rpc(url, method, params, **kw):
        return {"result": _result(100 if url.endswith("one") else 200)}
    monkeypatch.setattr(seams, "_rpc", rpc)

    def one(url):
        captured = {"result": "stale"}
        verdict = seams._rpc_eth_call_result(
            {"to": mixed.SMART_ROUTERS["56"]}, TAKER, url, result_out=captured)
        return verdict, captured

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(one, ["https://rpc.one", "https://rpc.two"]))
    assert [r[1]["result"] for r in results] == [_result(100), _result(200)]
    monkeypatch.setattr(seams, "_rpc", lambda *_a, **_k: {})
    assert one("https://rpc.one") == ((None, None, None), {})
