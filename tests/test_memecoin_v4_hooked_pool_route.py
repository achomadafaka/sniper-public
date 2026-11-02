"""A hooked V4 pool on 4663 executes only the PATH form of V4_SWAP (measured 2026-09-04).

HOVER 0xf7d2a651 on hook 0x4e3468951d: SWAP_EXACT_IN_SINGLE reverted with EMPTY data from our
address, a reference buyer's address and a fresh one, in every settle/take combination, at the
very block where a real buyer's bytes executed from our own address. Those bytes are
[SWAP_EXACT_IN, SETTLE(open, router pays), TAKE(open)] with a five-field ExactInputParams, and the
eth_abi encoding of that struct is byte-equal to them. Successful swaps used 752k-834k gas; ours
carried 700k. The Permit2-funded WETH branch had produced 0 of 59 V4 fills."""
from eth_abi import decode, encode
import pytest

import sniper.execution.evm_v4_quote as Q

WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
COIN = "0xf7d2a651188ea9f52cc802b3d962eb6c88bfaba3"
HOOK = "0x4e3468951d49f2eea976ed0d6e75ffcb44a9a544"
TAKER = "0x898be9c27f8b391c47a3d9f2da17781aa8aa3a1d"
FORK = "(address,(address,uint24,int24,address,bytes)[],bytes,uint128,uint128)"
HOOKED_WETH_KEY = {"currency0": WETH, "currency1": COIN, "fee": 0x800000, "tick_spacing": 200, "hooks": HOOK}
PLAIN_NATIVE_KEY = {"currency0": "0x" + "00" * 20, "currency1": COIN, "fee": 2500, "tick_spacing": 25, "hooks": "0x" + "00" * 20}
AMT, OUT = 61157840231258176, 24095309325775143415517060
V4RC_HOOK = "0x919eb369e51840f78b9f78e59c47620038bafacc"


def _unpack(hexstr):
    actions, params = decode(["bytes", "bytes[]"], bytes.fromhex(hexstr))
    return actions, params


def test_the_path_form_is_byte_equal_to_the_routers_five_field_struct():
    packed = Q._v4_swap_input_path(HOOKED_WETH_KEY, True, AMT, 123, payer_is_user=False, take_to=TAKER, chain="4663")
    actions, params = _unpack(packed)
    assert actions == bytes([0x07, 0x0B, 0x0E])
    expected = encode([FORK], [(WETH, [(COIN, 0x800000, 200, HOOK, b"")], b"", AMT, 123)])
    assert params[0] == expected, "SWAP_EXACT_IN params must match the deployed router's layout"
    assert decode(["address", "uint256", "bool"], params[1]) == (WETH, 0, False)
    assert decode(["address", "address", "uint256"], params[2]) == (WETH and COIN, TAKER, 0)


def test_the_path_form_reverses_direction_and_can_pay_through_the_permit():
    packed = Q._v4_swap_input_path(HOOKED_WETH_KEY, False, AMT, 0, payer_is_user=True, take_to=Q._ADDRESS_THIS, chain="4663")
    actions, params = _unpack(packed)
    (cin, path, extra, amount_in, min_out), = decode([FORK], params[0])
    assert (cin, amount_in, min_out, extra) == (COIN, AMT, 0, b"")
    assert path[0][0] == WETH and path[0][3] == HOOK
    assert decode(["address", "uint256", "bool"], params[1]) == (COIN, 0, True)
    assert decode(["address", "address", "uint256"], params[2])[1] == "0x" + "00" * 19 + "02"


def _quote(key, pool_id="0x" + "ff" * 32):
    return {"amount_out": OUT, "pool_id": pool_id, "pool_key": key, "zero_for_one": True,
            "quote_currency": key["currency0"]}


@pytest.mark.parametrize("chain,hook,gas", [
    ("4663", V4RC_HOOK, "2000000"), ("4663", HOOK, "1200000"),
    ("8453", V4RC_HOOK, "1200000"), ("1", V4RC_HOOK, "1200000")])
@pytest.mark.parametrize("branch", ["quote_buy", "quote_in_swap_buy", "quote_sell",
                                   "native_buy", "weth_buy"])
def test_verified_hook_gas_scope_reaches_every_builder(monkeypatch, chain, hook, gas, branch):
    from types import SimpleNamespace
    from sniper.execution.evm_wallet import CHAIN_QUOTE
    weth = CHAIN_QUOTE[chain]["token"].lower()
    key = dict(HOOKED_WETH_KEY, currency0=weth if branch == "weth_buy" else Q._NATIVE,
               hooks=hook)
    monkeypatch.setattr(Q, "_in_swap_unwrap_start", lambda *a, **k:
        (SimpleNamespace(result=lambda **kw: 3), None) if branch == "quote_in_swap_buy" else (None, None))
    monkeypatch.setattr(Q, "_permit2_input", lambda *a, **k: "ab" * 64)
    if branch in ("native_buy", "weth_buy"):
        tx = (Q.native_buy_tx(key, AMT, 123, 1_800_000_060, taker=TAKER, chain=chain)
              if branch == "native_buy" else
              Q.weth_buy_tx(key, True, AMT, 123, 1_800_000_060, taker=TAKER, chain=chain))
    else:
        sell = branch == "quote_sell"
        monkeypatch.setattr(Q, "v4_quote_amount_out", lambda *a, **k:
                            dict(_quote(key), zero_for_one=not sell))
        q = Q.dex_v4_quote(chain, COIN if sell else weth, weth if sell else COIN,
            AMT, TAKER, rpc_url="https://rpc", acct=object(), slippage_bps=200,
            now_fn=lambda: 1_800_000_000)
        assert q["minBuyAmount"] == str(OUT * 9800 // 10000)
        assert bool(q["route"].get("in_swap_unwrap")) == (branch == "quote_in_swap_buy")
        tx = q["transaction"]
    assert tx["gas"] == gas


def test_verified_hook_budget_is_shared_by_preflight_signing_and_affordability(monkeypatch):
    from types import SimpleNamespace
    from sniper.execution import evm_real_seams as S
    from sniper.execution.evm_native_float import float_covers
    key = dict(HOOKED_WETH_KEY, currency0=Q._NATIVE, hooks=V4RC_HOOK)
    monkeypatch.setattr(Q, "v4_quote_amount_out", lambda *a, **k: _quote(key))
    monkeypatch.setattr(Q, "_in_swap_unwrap_start", lambda *a, **k: (None, None))
    q = Q.dex_v4_quote("4663", WETH, COIN, AMT, TAKER, rpc_url="https://rpc",
                       now_fn=lambda: 1_800_000_000)
    calls, signed = [], []
    monkeypatch.setattr(S, "_rpc", lambda url, method, params, **kw:
                        calls.append(params[0]) or {"result": "0x"})
    assert S.preflight_weth_entry_quote(q, quote_token=WETH, taker=TAKER,
                                       rpc_url="https://rpc") == (True, "ok")
    monkeypatch.setattr(S, "eth_nonce", lambda *a, **k: 0)
    monkeypatch.setattr(S, "eth_gas_price", lambda *a, **k: 100)
    acct = SimpleNamespace(address=TAKER, sign_transaction=lambda tx:
        signed.append(tx) or SimpleNamespace(raw_transaction=b"\x01", hash=b"\x02"))
    executor = S.build_real_evm_executor(acct=acct, chain_id="4663", rpc_url="https://rpc")
    assert executor["_sign"](q["transaction"]) is not None
    assert calls[0]["gas"] == hex(2_000_000)
    assert signed[0]["gas"] == int(q["transaction"]["gas"]) == 2_000_000
    old_float = AMT + 1_200_000 * 100 * 3
    assert float_covers(old_float, AMT, 1_200_000, 100, 0)
    assert not float_covers(old_float, AMT, int(q["transaction"]["gas"]), 100, 0)


def test_a_hooked_weth_pool_buy_funds_the_router_with_native_and_carries_hooked_gas(monkeypatch):
    monkeypatch.setattr(Q, "v4_quote_amount_out", lambda *a, **k: _quote(HOOKED_WETH_KEY))
    q = Q.dex_v4_quote("4663", WETH, COIN, AMT, TAKER, rpc_url="https://rpc", now_fn=lambda: 1_800_000_000)
    assert q is not None
    tx = q["transaction"]
    commands, inputs, _deadline = decode(["bytes", "bytes[]", "uint256"], bytes.fromhex(tx["data"][10:]))
    assert commands == bytes([Q._CMD_WRAP_ETH, Q._CMD_V4_SWAP]), "WRAP_ETH into the router, then swap"
    assert decode(["uint256", "uint256"], inputs[0]) == (Q._ADDRESS_THIS, AMT)
    actions, params = decode(["bytes", "bytes[]"], inputs[1])
    assert actions == bytes([0x07, 0x0B, 0x0E])
    assert decode(["address", "uint256", "bool"], params[1]) == (WETH, 0, False)
    assert tx["value"] == hex(AMT) and tx["gas"] == Q._HOOKED_POOL_GAS
    assert len(q["pre_transactions"]) == 1 and Q._SEL_WETH_WITHDRAW in q["pre_transactions"][0]["data"]
    assert q["pre_transactions"][0]["gas"] == str(Q.WETH_CALL_GAS_LIMIT)
    assert q["issues"]["allowance"] is None, "no Permit2 on this path"
    assert q["route"]["hooked"] is True


def test_a_plain_native_pool_buy_is_unchanged(monkeypatch):
    monkeypatch.setattr(Q, "v4_quote_amount_out", lambda *a, **k: _quote(PLAIN_NATIVE_KEY))
    q = Q.dex_v4_quote("4663", WETH, COIN, AMT, TAKER, rpc_url="https://rpc", now_fn=lambda: 1_800_000_000)
    commands, inputs, _ = decode(["bytes", "bytes[]", "uint256"], bytes.fromhex(q["transaction"]["data"][10:]))
    assert commands == bytes([Q._CMD_V4_SWAP])
    actions, _params = decode(["bytes", "bytes[]"], inputs[0])
    assert actions == bytes([0x06, 0x0C, 0x0F]), "single-hop stays SINGLE + SETTLE_ALL + TAKE_ALL"
    assert q["transaction"]["gas"] == "700000" and "hooked" not in q["route"]


def test_a_hooked_weth_pool_sell_mirrors_the_reference_sell(monkeypatch):
    sell_key = dict(HOOKED_WETH_KEY)
    monkeypatch.setattr(Q, "v4_quote_amount_out", lambda *a, **k: {**_quote(sell_key), "zero_for_one": False, "quote_currency": WETH})
    monkeypatch.setattr(Q, "_permit2_input", lambda *a, **k: "ab" * 64)
    q = Q.dex_v4_quote("4663", COIN, WETH, 10 ** 24, TAKER, rpc_url="https://rpc", acct=object(), now_fn=lambda: 1_800_000_000)
    assert q is not None
    commands, inputs, _ = decode(["bytes", "bytes[]", "uint256"], bytes.fromhex(q["transaction"]["data"][10:]))
    assert commands == bytes([Q._CMD_PERMIT2_PERMIT, Q._CMD_V4_SWAP])
    actions, params = decode(["bytes", "bytes[]"], inputs[1])
    assert actions == bytes([0x07, 0x0B, 0x0E])
    assert decode(["address", "uint256", "bool"], params[1]) == (COIN, 0, True), "we pay through the permit"
    assert decode(["address", "address", "uint256"], params[2]) == (WETH, TAKER, 0)
    assert q["transaction"]["gas"] == Q._HOOKED_POOL_GAS and q["issues"]["allowance"]["spender"]


def test_pool_not_initialized_from_the_simulation_is_retried():
    import sniper.services.memecoin_real_runner as R
    assert R._is_transient_entry_miss({"status": "route_failed",
        "reason": "entry_simulation_reverted: exact route reverted; detail=custom_error 0x486aa307"})
    assert not R._is_transient_entry_miss({"status": "route_failed",
        "reason": "entry_simulation_reverted: exact route reverted; detail=execution reverted"})


UPSTREAM = "(address,(address,uint24,int24,address,bytes)[],uint128,uint128)"


def test_base_and_mainnet_encode_upstreams_four_field_struct():
    # OPH 67763 on Base: the five-field bytes reverted V4TooLittleReceived(<balance>, 0); the same
    # sell with four fields executes on Base state (eth_call, 2026-10-01).
    for chain in ("8453", "1"):
        packed = Q._v4_swap_input_path(HOOKED_WETH_KEY, False, AMT, 7, payer_is_user=True,
                                       take_to=TAKER, chain=chain)
        actions, params = _unpack(packed)
        assert actions == bytes([0x07, 0x0B, 0x0E])
        assert params[0] == encode([UPSTREAM], [(COIN, [(WETH, 0x800000, 200, HOOK, b"")], AMT, 7)])


def test_every_other_chain_keeps_the_five_field_bytes():
    for chain in ("4663", "56", "5042"):
        packed = Q._v4_swap_input_path(HOOKED_WETH_KEY, False, AMT, 7, payer_is_user=True,
                                       take_to=TAKER, chain=chain)
        _, params = _unpack(packed)
        assert params[0] == encode([FORK], [(COIN, [(WETH, 0x800000, 200, HOOK, b"")], b"", AMT, 7)])


def test_alternates_ride_only_on_a_discovered_buy(monkeypatch):
    # KLN 2026-10-08: the ladder's over-budget fallback reads the other quoted pools off the buy.
    # A rebuild on a named pool carries none, and an exit quote stays byte-identical.
    alt = dict(_quote(PLAIN_NATIVE_KEY, pool_id="0x" + "ee" * 32), amount_out=OUT - 1)
    monkeypatch.setattr(Q, "v4_quote_amount_out", lambda *a, **k: {**_quote(PLAIN_NATIVE_KEY), "alternates": [alt]})
    buy = Q.dex_v4_quote("4663", WETH, COIN, AMT, TAKER, rpc_url="https://rpc", now_fn=lambda: 1_800_000_000)
    assert buy["_v4_alternates"] == [alt]
    rebuilt = Q.dex_v4_quote("4663", WETH, COIN, AMT, TAKER, rpc_url="https://rpc", now_fn=lambda: 1_800_000_000,
                             quote_override=alt)
    assert "_v4_alternates" not in rebuilt and rebuilt["route"]["pool_id"] == alt["pool_id"]
    assert rebuilt["buyAmount"] == str(OUT - 1)
    monkeypatch.setattr(Q, "v4_quote_amount_out", lambda *a, **k: {
        **_quote(PLAIN_NATIVE_KEY), "zero_for_one": False, "alternates": [alt]})
    monkeypatch.setattr(Q, "_permit2_input", lambda *a, **k: "ab" * 64)
    sell = Q.dex_v4_quote("4663", COIN, WETH, 10 ** 24, TAKER, rpc_url="https://rpc", acct=object(),
                          now_fn=lambda: 1_800_000_000)
    assert sell is not None and "_v4_alternates" not in sell
