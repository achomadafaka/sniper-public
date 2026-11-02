"""Direct Uniswap-V3 quote + swap-tx builder for chain 4663 (Robinhood), 2026-07-16.

The 0x API key is dead, so the real EVM path talks to the chain's Uniswap V3 deployment directly:
QuoterV2 for an EXACT amountOut, SwapRouter02 `exactInputSingle` for the swap tx. The returned dict is
shaped EXACTLY like `zerox_quote`'s output so the existing EVM executor orchestration is reused
verbatim. Addresses/selectors verified on-chain — see
docs/research/2026-07-16-robinhood-4663-dex-venue.md. Spec:
docs/superpowers/specs/2026-07-16-memecoin-direct-swap-4663-design.md (Phase B).
"""
from sniper.execution import evm_dex_quote as Q
from eth_abi import decode

QUOTER = "0x33e885ed0ec9bf04ecfb19341582aadcb4c8a9e7"
ROUTER = "0xcaf681a66d020601342297493863e78c959e5cb2"
WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
TOKEN = "0x020bfc650a365f8bb26819deaabf3e21291018b4"
ADDR = {"router": ROUTER, "quoter": QUOTER}

QUOTE_SELECTOR = "c6a5026a"       # QuoterV2.quoteExactInputSingle
SWAP_SELECTOR = "04e45aaf"        # SwapRouter02.exactInputSingle
QUOTE_PATH_SELECTOR = "cdca1753"  # QuoterV2.quoteExactInput
SWAP_PATH_SELECTOR = "b858183f"   # SwapRouter02.exactInput

BASE_USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
BASE_WETH = "0x4200000000000000000000000000000000000006"
BASE_TOKEN = "0xd769d56f479e9e72a77bb1523e866a33098feec5"
# BASE CASH IS WETH AND THE CONNECTOR IS USDC (2026-09-06). One endpoint of a two-hop
# rung must be the chain's own quote token -- `dex_v3_quote` skips every other shape --
# so the Base rung reversed when the bankroll moved from USDC to WETH.
PONS = "0x39dbed3a2bd333467115de45665cc57f813c4571"
TAM = "0x875034570e0a5670d04d028f08964f1814cdd71e"


def _fee_from_quoter_calldata(data):
    """QuoterV2 struct: (tokenIn, tokenOut, amountIn, fee, sqrtLimit) — fee is the 4th word."""
    body = data[10:]                            # strip 0x + 4-byte selector
    return int(body[3 * 64:4 * 64], 16)


def _word(data_no0x, i):
    return int(data_no0x[i * 64:(i + 1) * 64], 16)


def _make_call_fn(amount_by_fee):
    """Fake eth_call: QuoterV2 returns a 4-word (amountOut, ...) response for fee tiers that have a
    pool; a tier with no pool 'reverts' -> None (exactly how the real quoter behaves)."""
    def call_fn(to, data):
        assert to.lower() == QUOTER.lower()
        assert data[2:10] == QUOTE_SELECTOR
        fee = _fee_from_quoter_calldata(data)
        out = amount_by_fee.get(fee)
        if out is None:
            return None                         # no pool at this fee tier
        return "0x" + format(out, "064x") + "00" * 96   # amountOut + 3 padding words
    return call_fn


def test_quote_returns_a_zerox_shaped_executable_dict():
    q = Q.dex_v3_quote("4663", WETH, TOKEN, 10 ** 14, ROUTER,
                       slippage_bps=100, cfg_addr=ADDR,
                       call_fn=_make_call_fn({3000: 2_000_000}), fee_tiers=(500, 3000, 10000))
    assert q["sellAmount"] == str(10 ** 14)
    assert q["buyAmount"] == str(2_000_000)
    assert q["liquidityAvailable"] is True
    # executor reads issues.allowance.spender (approve target) + transaction (to/data/value)
    assert q["issues"]["allowance"]["spender"].lower() == ROUTER.lower()
    assert q["transaction"]["to"].lower() == ROUTER.lower()
    assert int(str(q["transaction"]["value"]), 16) == 0
    # tx calls SwapRouter02.exactInputSingle
    assert q["transaction"]["data"][2:10] == SWAP_SELECTOR


def test_quote_encodes_exact_input_single_params_correctly():
    q = Q.dex_v3_quote("4663", WETH, TOKEN, 12345, ROUTER,
                       slippage_bps=100, cfg_addr=ADDR,
                       call_fn=_make_call_fn({3000: 1_000_000}), fee_tiers=(3000,))
    body = q["transaction"]["data"][10:]        # strip 0x + selector
    # struct: (tokenIn, tokenOut, fee, recipient, amountIn, amountOutMinimum, sqrtPriceLimitX96)
    assert ("0x%040x" % _word(body, 0)) == WETH          # tokenIn
    assert ("0x%040x" % _word(body, 1)) == TOKEN         # tokenOut
    assert _word(body, 2) == 3000                        # fee
    assert ("0x%040x" % _word(body, 3)) == ROUTER.lower()  # recipient = taker
    assert _word(body, 4) == 12345                       # amountIn
    assert _word(body, 5) == 1_000_000 * 9900 // 10000   # amountOutMinimum = out*(1-slip)
    assert _word(body, 6) == 0                           # sqrtPriceLimitX96


def test_quote_picks_the_best_fee_tier():
    """Scan every configured tier; the deepest (largest amountOut) wins, and the swap tx is built for
    THAT fee — a coin can be pooled on more than one tier (CASHCAT 0.3%, Index 1%)."""
    q = Q.dex_v3_quote("4663", WETH, TOKEN, 10 ** 14, ROUTER,
                       slippage_bps=100, cfg_addr=ADDR,
                       call_fn=_make_call_fn({3000: 1_000, 10000: 5_000}),
                       fee_tiers=(500, 3000, 10000))
    assert q["buyAmount"] == str(5_000)
    body = q["transaction"]["data"][10:]
    assert _word(body, 2) == 10000              # best tier baked into the swap tx
    assert q["route"] == {
        "protocol": "uniswap_v3", "tokens": [WETH.lower(), TOKEN.lower()], "fees": [10000],
    }


def test_quote_fail_closed_when_no_pool_on_any_tier():
    q = Q.dex_v3_quote("4663", WETH, TOKEN, 10 ** 14, ROUTER, slippage_bps=100, cfg_addr=ADDR,
                       call_fn=_make_call_fn({}), fee_tiers=(500, 3000, 10000))
    assert q is None


def test_quote_fail_closed_on_rpc_error():
    def boom(to, data):
        raise RuntimeError("rpc 500")
    q = Q.dex_v3_quote("4663", WETH, TOKEN, 10 ** 14, ROUTER, slippage_bps=100, cfg_addr=ADDR,
                       call_fn=boom, fee_tiers=(3000,))
    assert q is None


def test_quote_fail_closed_on_zero_amount_out():
    q = Q.dex_v3_quote("4663", WETH, TOKEN, 10 ** 14, ROUTER, slippage_bps=100, cfg_addr=ADDR,
                       call_fn=_make_call_fn({3000: 0}), fee_tiers=(3000,))
    assert q is None


def test_quote_fail_closed_when_chain_has_no_configured_addresses():
    """A chain without verified V3 addresses must never produce a direct quote (fail-closed)."""
    q = Q.dex_v3_quote("8453", WETH, TOKEN, 10 ** 14, ROUTER, slippage_bps=100, cfg_addr=None,
                       call_fn=_make_call_fn({3000: 1}), fee_tiers=(3000,))
    assert q is None


def _path_bytes(tokens, fees):
    raw = bytes.fromhex(tokens[0][2:])
    for fee, token in zip(fees, tokens[1:]):
        raw += int(fee).to_bytes(3, "big") + bytes.fromhex(token[2:])
    return raw


def test_quote_falls_back_to_configured_two_hop_path_after_direct_miss():
    expected_path = _path_bytes((BASE_WETH, BASE_USDC, BASE_TOKEN), (500, 10000))
    calls = []

    def call_fn(to, data):
        calls.append(data[2:10])
        if data[2:10] == QUOTE_SELECTOR:
            return None
        assert data[2:10] == QUOTE_PATH_SELECTOR
        path, amount = decode(["bytes", "uint256"], bytes.fromhex(data[10:]))
        assert path == expected_path and amount == 1_000_000
        return "0x" + format(2_000_000, "064x") + "00" * 128

    q = Q.dex_v3_quote(
        "8453", BASE_WETH, BASE_TOKEN, 1_000_000, ROUTER,
        slippage_bps=300, cfg_addr=ADDR, call_fn=call_fn,
        fee_tiers=(10000,), connectors=((BASE_USDC, 500),),
    )

    assert calls == [QUOTE_SELECTOR, QUOTE_PATH_SELECTOR]
    assert q["buyAmount"] == "2000000"
    assert q["transaction"]["data"][2:10] == SWAP_PATH_SELECTOR
    (params,) = decode(
        ["(bytes,address,uint256,uint256)"],
        bytes.fromhex(q["transaction"]["data"][10:]),
    )
    path, recipient, amount_in, amount_out_min = params
    assert path == expected_path
    assert recipient.lower() == ROUTER.lower()
    assert amount_in == 1_000_000
    assert amount_out_min == 2_000_000 * 9700 // 10000
    assert q["route"] == {
        "protocol": "uniswap_v3",
        "tokens": [BASE_WETH.lower(), BASE_USDC.lower(), BASE_TOKEN.lower()],
        "fees": [500, 10000],
    }


def test_two_hop_quote_reverses_the_exact_connector_path_for_exits():
    expected_path = _path_bytes((BASE_TOKEN, BASE_USDC, BASE_WETH), (10000, 500))

    def call_fn(_to, data):
        if data[2:10] == QUOTE_SELECTOR:
            return None
        path, amount = decode(["bytes", "uint256"], bytes.fromhex(data[10:]))
        assert path == expected_path and amount == 2_000_000
        return "0x" + format(979_100, "064x") + "00" * 128

    q = Q.dex_v3_quote(
        "8453", BASE_TOKEN, BASE_WETH, 2_000_000, ROUTER,
        slippage_bps=300, cfg_addr=ADDR, call_fn=call_fn,
        fee_tiers=(10000,), connectors=((BASE_USDC, 500),),
    )

    assert q["buyAmount"] == "979100"
    (params,) = decode(
        ["(bytes,address,uint256,uint256)"],
        bytes.fromhex(q["transaction"]["data"][10:]),
    )
    assert params[0] == expected_path


def test_4663_pons_connector_builds_symmetric_v3_route():
    buy_path = _path_bytes((WETH, PONS, TAM), (3000, 10000))
    sell_path = _path_bytes((TAM, PONS, WETH), (10000, 3000))

    def call_fn(_to, data):
        if data[2:10] == QUOTE_SELECTOR:
            return None
        path, _amount = decode(["bytes", "uint256"], bytes.fromhex(data[10:]))
        amount = {buy_path: 60_257_641_902_876_336_897_973,
                  sell_path: 11_448_389_454_497_685}.get(path)
        return None if amount is None else "0x" + format(amount, "064x") + "00" * 128

    buy = Q.dex_v3_quote(
        "4663", WETH, TAM, 12_128_806_218_902_446, ROUTER,
        slippage_bps=300, cfg_addr=ADDR, call_fn=call_fn,
        fee_tiers=(10000,), connectors=None,
    )
    sell = Q.dex_v3_quote(
        "4663", TAM, WETH, 58_449_912_645_790_046_791_033, ROUTER,
        slippage_bps=300, cfg_addr=ADDR, call_fn=call_fn,
        fee_tiers=(10000,), connectors=None,
    )

    assert buy["route"] == {
        "protocol": "uniswap_v3",
        "tokens": [WETH, PONS, TAM],
        "fees": [3000, 10000],
    }
    assert sell["route"] == {
        "protocol": "uniswap_v3",
        "tokens": [TAM, PONS, WETH],
        "fees": [10000, 3000],
    }


def test_quote_stays_fail_closed_when_direct_and_connector_paths_miss():
    q = Q.dex_v3_quote(
        "8453", BASE_WETH, BASE_TOKEN, 1_000_000, ROUTER,
        slippage_bps=300, cfg_addr=ADDR, call_fn=lambda _to, _data: None,
        fee_tiers=(10000,), connectors=((BASE_USDC, 500),),
    )
    assert q is None


def test_quote_compares_direct_and_connector_outputs_and_uses_the_deeper_route():
    expected_path = _path_bytes((BASE_WETH, BASE_USDC, BASE_TOKEN), (500, 10000))

    def call_fn(_to, data):
        if data[2:10] == QUOTE_SELECTOR:
            return "0x" + format(1_000_000, "064x") + "00" * 96
        path, _amount = decode(["bytes", "uint256"], bytes.fromhex(data[10:]))
        assert path == expected_path
        return "0x" + format(2_000_000, "064x") + "00" * 128

    q = Q.dex_v3_quote(
        "8453", BASE_WETH, BASE_TOKEN, 1_000_000, ROUTER,
        slippage_bps=300, cfg_addr=ADDR, call_fn=call_fn,
        fee_tiers=(10000,), connectors=((BASE_USDC, 500),),
    )

    assert q["buyAmount"] == "2000000"
    assert q["transaction"]["data"][2:10] == SWAP_PATH_SELECTOR


def test_route_scan_uses_one_rpc_batch_when_the_transport_supports_it():
    batches = []

    def call_fn(_to, _data):
        raise AssertionError("route scan should use the batch transport")

    def batch_fn(calls):
        batches.append(calls)
        results = []
        for _to, data in calls:
            out = 2_000_000 if data[2:10] == QUOTE_PATH_SELECTOR else 1_000_000
            results.append("0x" + format(out, "064x") + "00" * 128)
        return results

    call_fn.batch = batch_fn
    q = Q.dex_v3_quote(
        "8453", BASE_WETH, BASE_TOKEN, 1_000_000, ROUTER,
        slippage_bps=300, cfg_addr=ADDR, call_fn=call_fn,
        fee_tiers=(3000, 10000), connectors=((BASE_USDC, 500),),
    )

    assert len(batches) == 1
    assert len(batches[0]) == 4
    assert q["buyAmount"] == "2000000"
    assert q["transaction"]["data"][2:10] == SWAP_PATH_SELECTOR


def test_route_scan_falls_back_to_single_calls_when_rpc_rejects_batch():
    calls = []

    def call_fn(_to, data):
        calls.append(data)
        return "0x" + format(1_000_000, "064x") + "00" * 96

    call_fn.batch = lambda _calls: None
    q = Q.dex_v3_quote(
        "4663", WETH, TOKEN, 100, ROUTER, cfg_addr=ADDR,
        call_fn=call_fn, fee_tiers=(3000,), connectors=(),
    )

    assert q is not None
    assert len(calls) == 1


def test_default_rpc_transport_retries_one_transient_failure(monkeypatch):
    calls = []
    monkeypatch.setenv("MEMECOIN_EVM_RPC_URL_8453", "https://rpc")

    def http(_method, _url, body=None):
        calls.append(body)
        if len(calls) == 1:
            raise RuntimeError("HTTP Error 429")
        return 200, {"jsonrpc": "2.0", "id": 1, "result": "0x01"}, None

    call_fn = Q._make_call_fn("8453", http)
    assert call_fn(QUOTER, "0x1234") == "0x01"
    assert len(calls) == 2


def test_default_rpc_batch_transport_retries_one_transient_failure(monkeypatch):
    calls = []
    monkeypatch.setenv("MEMECOIN_EVM_RPC_URL_8453", "https://rpc")

    def http(_method, _url, body=None):
        calls.append(body)
        if len(calls) == 1:
            raise RuntimeError("HTTP Error 429")
        return 200, [{"jsonrpc": "2.0", "id": 1, "result": "0x02"}], None

    call_fn = Q._make_call_fn("8453", http)
    assert call_fn.batch([(QUOTER, "0x1234")]) == ["0x02"]
    assert len(calls) == 2


def test_default_rpc_batch_uses_a_verified_pool_fallback_after_a_rate_limit(monkeypatch):
    import json
    from sniper.execution import evm_rpc_pool
    import sniper.settings.memecoin as cfg

    primary, fallback = "https://primary", "https://publicnode"
    monkeypatch.setenv("MEMECOIN_EVM_RPC_URL_4663", primary)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RPC_POOL_URLS", {"4663": (primary, fallback)})
    evm_rpc_pool.reset_for_tests()
    calls = []

    def http(_method, url, body=None):
        request = json.loads(body)
        calls.append((url, request))
        if isinstance(request, dict) and request["method"] == "eth_chainId":
            return 200, {"jsonrpc": "2.0", "id": 1, "result": "0x1237"}, None
        if url == primary:
            return 429, None, "rate limited"
        return 200, [
            {"jsonrpc": "2.0", "id": item["id"], "result": "0x02"}
            for item in request
        ], None

    call_fn = Q._make_call_fn("4663", http)
    assert call_fn.batch([(QUOTER, "0x1234"), (QUOTER, "0x5678")]) == ["0x02", "0x02"]
    assert [url for url, _request in calls] == [primary, primary, fallback, fallback]
    assert calls[0][1]["method"] == "eth_chainId"
    assert isinstance(calls[1][1], list) and len(calls[1][1]) == 2
    assert calls[2][1]["method"] == "eth_chainId"
    assert isinstance(calls[3][1], list) and len(calls[3][1]) == 2


def test_persistent_batch_transport_failure_does_not_fan_out_single_calls(monkeypatch):
    """No per-tier fallback storm, and the failure is now REPORTED rather than returned as None.

    This asserted `q is None` until 2026-09-08. That was the defect: the ladder reads a None with no
    blindness as a TERMINAL `no_route`, so a persistent 429 on the primary rung published "this coin
    has no v3 route" and was never re-quoted. The bounded-retry count is the real subject of this
    test and is unchanged.
    """
    import pytest as _pytest
    calls = []
    monkeypatch.setenv("MEMECOIN_EVM_RPC_URL_8453", "https://rpc")
    def http(_method, _url, body=None):
        calls.append(body)
        raise RuntimeError("HTTP Error 429")

    with _pytest.raises(Q.V3ReadUnavailable) as exc:
        Q.dex_v3_quote(
            "8453", BASE_WETH, BASE_TOKEN, 1_000_000, ROUTER,
            cfg_addr=ADDR, http=http, fee_tiers=(3000, 10000), connectors=(),
        )

    assert exc.value.failure_type == "rpc_transport"
    assert len(calls) == 2  # one batch + one bounded retry; no per-tier fallback storm


# --- multiple V3 deployments per chain (2026-08-17, CLOSED 0x034d81…) ------------------------

QUOTER2 = "0x3e290e5e01818002a0b672148bdc7514d861c7b3"
ROUTER2 = "0x51d0e5188afe12d502e29d982d20c190e7816107"
ADDR2 = {"router": ROUTER2, "quoter": QUOTER2}
RAMSES_QUOTER = "0x4730e03eb4a58a5e20244062d5f9a99bcf5770a6"
RAMSES_ROUTER = "0xfcbbe2af83f94e7e2a9c35a535b3a04719afd2ae"
RAMSES_ADDR = {"router": RAMSES_ROUTER, "quoter": RAMSES_QUOTER, "protocol": "ramses_v3"}
RAMSES_QUOTE_SELECTOR = "9e7defe6"
RAMSES_SWAP_SELECTOR = "a026383e"


def _make_multi_call_fn(amount_by_quoter_fee):
    """Fake eth_call across TWO quoters: {(quoter_lower, fee): amountOut}."""
    def call_fn(to, data):
        assert data[2:10] == QUOTE_SELECTOR
        fee = _fee_from_quoter_calldata(data)
        out = amount_by_quoter_fee.get((to.lower(), fee))
        return None if out is None else "0x" + format(out, "064x") + "00" * 96
    return call_fn


def test_second_deployment_quotes_when_the_first_has_no_pool():
    """The CLOSED case: every tier reverts on the primary quoter; the second deployment holds
    the pool. The swap must build against the SECOND deployment's router."""
    call_fn = _make_multi_call_fn({(QUOTER2.lower(), 10000): 41_892_000_000})
    q = Q.dex_v3_quote("4663", WETH, TOKEN, 10 ** 14, ROUTER,
                       slippage_bps=100, cfg_addr=[ADDR, ADDR2],
                       call_fn=call_fn, fee_tiers=(3000, 10000), connectors=())
    assert q is not None
    assert q["transaction"]["to"] == ROUTER2
    assert q["issues"]["allowance"]["spender"] == ROUTER2
    assert int(q["buyAmount"]) == 41_892_000_000


def test_deepest_deployment_wins_when_both_quote():
    call_fn = _make_multi_call_fn({(QUOTER.lower(), 3000): 100, (QUOTER2.lower(), 3000): 90})
    q = Q.dex_v3_quote("4663", WETH, TOKEN, 10 ** 14, ROUTER,
                       slippage_bps=100, cfg_addr=[ADDR, ADDR2],
                       call_fn=call_fn, fee_tiers=(3000,), connectors=())
    assert q["transaction"]["to"] == ROUTER      # primary is deeper -> primary router
    assert int(q["buyAmount"]) == 100


def test_single_dict_cfg_addr_stays_backward_compatible():
    call_fn = _make_multi_call_fn({(QUOTER.lower(), 3000): 55})
    q = Q.dex_v3_quote("4663", WETH, TOKEN, 10 ** 14, ROUTER,
                       slippage_bps=100, cfg_addr=ADDR,
                       call_fn=call_fn, fee_tiers=(3000,), connectors=())
    assert q["transaction"]["to"] == ROUTER and int(q["buyAmount"]) == 55


def test_ramses_deployment_quotes_by_tick_spacing_and_builds_its_router():
    """Ramses V3 uses int24 tickSpacing plus a deadline. It is not a Uniswap V3 deployment."""
    amount_in = 10 ** 14
    tick_spacings = []

    def call_fn(to, data):
        assert to.lower() == RAMSES_QUOTER
        assert data[2:10] == RAMSES_QUOTE_SELECTOR
        body = data[10:]
        assert _word(body, 2) == amount_in
        tick_spacing = _word(body, 3)
        tick_spacings.append(tick_spacing)
        return ("0x" + format(2_000_000, "064x") + "00" * 96
                if tick_spacing == 100 else None)

    q = Q.dex_v3_quote(
        "4663", WETH, TOKEN, amount_in, ROUTER,
        slippage_bps=100, cfg_addr=RAMSES_ADDR, call_fn=call_fn,
        fee_tiers=(), connectors=(), now_fn=lambda: 1_000,
    )

    assert q["transaction"]["to"] == RAMSES_ROUTER
    assert q["issues"]["allowance"]["spender"] == RAMSES_ROUTER
    assert q["transaction"]["data"][2:10] == RAMSES_SWAP_SELECTOR
    body = q["transaction"]["data"][10:]
    assert _word(body, 2) == 100
    assert _word(body, 4) == 1_120
    assert _word(body, 5) == amount_in
    assert tick_spacings == [1, 5, 10, 50, 100, 200]
    assert q["route"] == {
        "protocol": "ramses_v3",
        "tokens": [WETH, TOKEN],
        "tick_spacings": [100],
    }


def test_the_v2_v3_router_transaction_carries_its_own_gas_limit():
    """MEMECOIN_EVM_DEFAULT_ROUTE_GAS (900,000). Gamma's sweep of every funded EVM fill 09-16..09-30: the signer's 500,000 default applied to this
    builder's transaction, and the tightest measured use was 382,080 (56 pcs_mixed_v3_v2)."""
    q = Q.dex_v3_quote("4663", WETH, TOKEN, 10 ** 14, ROUTER,
                       slippage_bps=100, cfg_addr=ADDR,
                       call_fn=_make_call_fn({3000: 2_000_000}), fee_tiers=(500, 3000, 10000))
    assert q["transaction"]["gas"] == "900000"
