"""Exit-only Uniswap V2 fallback for fee-on-transfer chain-4663 bags."""

import time as stdlib_time

from sniper.execution import evm_v2_exit_quote as Q


ROUTER = "0x" + "11" * 20
TAKER = "0x" + "22" * 20
TOKEN = "0x" + "33" * 20
CONNECTOR = "0x" + "44" * 20
WETH = "0x" + "55" * 20

GET_AMOUNTS_OUT = "d06ca61f"
SWAP_FOT = "5c11d795"


def _word(body, index):
    return int(body[index * 64:(index + 1) * 64], 16)


def _path_from_get_amounts(data):
    assert data[2:10] == GET_AMOUNTS_OUT
    body = data[10:]
    count = _word(body, 2)
    return tuple("0x" + body[(3 + i) * 64 + 24:(4 + i) * 64] for i in range(count))


def _amount_from_get_amounts(data):
    return _word(data[10:], 0)


def _amounts_result(amounts):
    return ("0x" + format(32, "064x") + format(len(amounts), "064x")
            + "".join(format(x, "064x") for x in amounts))


def _call_with_routes(outputs, seen):
    def call_fn(to, data):
        assert to.lower() == ROUTER.lower()
        path = _path_from_get_amounts(data)
        quoted_in = _amount_from_get_amounts(data)
        seen.append((path, quoted_in))
        out = outputs.get(path)
        if out is None:
            return None
        if len(path) == 2:
            return _amounts_result([quoted_in, out])
        return _amounts_result([quoted_in, out // 2, out])

    return call_fn


def test_v2_exit_scans_connector_and_builds_fee_on_transfer_swap():
    seen = []
    direct = (TOKEN.lower(), WETH.lower())
    connected = (TOKEN.lower(), CONNECTOR.lower(), WETH.lower())

    quote = Q.dex_v2_exit_quote(
        "4663", TOKEN, WETH, 1_000, TAKER,
        slippage_bps=2_500, cfg_router=ROUTER,
        connector_tokens=(CONNECTOR,), tax_buffer_bps=1_000,
        call_fn=_call_with_routes({direct: 700, connected: 800}, seen),
        now_fn=lambda: 1_000,
    )

    assert seen == [(direct, 900), (connected, 900)]
    assert quote["sellAmount"] == "1000"          # approve/sell the exact full balance
    assert quote["buyAmount"] == "800"            # conservative post-tax route quote
    assert quote["issues"]["allowance"]["spender"].lower() == ROUTER.lower()
    assert quote["transaction"]["to"].lower() == ROUTER.lower()
    assert quote["transaction"]["value"] == "0x0"
    assert quote["route"] == {
        "protocol": "uniswap_v2", "router": ROUTER.lower(),
        "tokens": list(connected), "path": list(connected),
    }

    data = quote["transaction"]["data"]
    assert data[2:10] == SWAP_FOT
    body = data[10:]
    assert _word(body, 0) == 1_000                  # full fee-on-transfer amount enters router
    assert _word(body, 1) == 600                    # 800 less existing 25% exit slippage
    assert _word(body, 2) == 160                    # dynamic path offset
    assert _word(body, 4) == 1_300                  # bounded deadline
    assert _word(body, 5) == 3


def test_v2_exit_default_deadline_uses_corrected_real_utc(monkeypatch):
    direct = (TOKEN.lower(), WETH.lower())
    monkeypatch.setattr(Q, "real_utc_now", lambda: 1_000, raising=False)
    monkeypatch.setattr(stdlib_time, "time", lambda: 999_999)

    quote = Q.dex_v2_exit_quote(
        "4663", TOKEN, WETH, 1_000, TAKER,
        slippage_bps=100, cfg_router=ROUTER,
        connector_tokens=(), tax_buffer_bps=0,
        call_fn=_call_with_routes({direct: 700}, []),
    )

    assert _word(quote["transaction"]["data"][10:], 4) == 1_300


def test_v2_exit_skips_duplicate_connector_paths():
    seen = []
    direct = (TOKEN.lower(), WETH.lower())
    Q.dex_v2_exit_quote(
        "4663", TOKEN, WETH, 1_000, TAKER,
        slippage_bps=100, cfg_router=ROUTER,
        connector_tokens=(TOKEN, WETH, TOKEN), tax_buffer_bps=0,
        call_fn=_call_with_routes({direct: 700}, seen), now_fn=lambda: 1_000,
    )
    assert seen == [(direct, 1_000)]


def test_v2_exit_fails_closed_when_no_path_quotes():
    assert Q.dex_v2_exit_quote(
        "4663", TOKEN, WETH, 1_000, TAKER,
        slippage_bps=100, cfg_router=ROUTER,
        connector_tokens=(CONNECTOR,), tax_buffer_bps=1_000,
        call_fn=lambda _to, _data: None,
    ) is None


def test_v2_exit_fails_closed_on_rpc_exception():
    def boom(_to, _data):
        raise RuntimeError("rpc down")

    assert Q.dex_v2_exit_quote(
        "4663", TOKEN, WETH, 1_000, TAKER,
        slippage_bps=100, cfg_router=ROUTER,
        connector_tokens=(), tax_buffer_bps=1_000, call_fn=boom,
    ) is None


def test_v2_exit_rejects_invalid_amounts_and_bps():
    for amount, slippage, tax in ((0, 100, 100), (1_000, 10_000, 100),
                                  (1_000, 100, 10_000), (1_000, -1, 100)):
        assert Q.dex_v2_exit_quote(
            "4663", TOKEN, WETH, amount, TAKER,
            slippage_bps=slippage, cfg_router=ROUTER,
            connector_tokens=(), tax_buffer_bps=tax,
            call_fn=lambda _to, _data: _amounts_result([1, 1]),
        ) is None


def test_the_fee_on_transfer_swap_carries_its_own_gas_limit(monkeypatch):
    """MEMECOIN_EVM_DEFAULT_ROUTE_GAS (900,000), read at call time. Gamma's sweep of every funded EVM fill 09-16..09-30: the signer's 500,000 default applied to this
    builder's transaction, and the tightest measured use was 382,080 (56 pcs_mixed_v3_v2)."""
    from sniper.settings import memecoin as cfg

    def quote():
        return Q.dex_v2_exit_quote(
            "4663", TOKEN, WETH, 1_000, TAKER, slippage_bps=2_500, cfg_router=ROUTER,
            connector_tokens=(), tax_buffer_bps=0,
            call_fn=_call_with_routes({(TOKEN.lower(), WETH.lower()): 700}, []), now_fn=lambda: 1_000)
    assert quote()["transaction"]["gas"] == "900000"
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_DEFAULT_ROUTE_GAS", 1_100_000)
    assert quote()["transaction"]["gas"] == "1100000"


def test_lowering_the_signed_minimum_changes_that_word_only():
    """Tax-aware entry minimum (2026-09-30): no re-quote, no read; one calldata word."""
    quote = Q.dex_v2_exit_quote(
        "4663", WETH, TOKEN, 1_000, TAKER, slippage_bps=300, cfg_router=ROUTER,
        connector_tokens=(), tax_buffer_bps=0,
        call_fn=_call_with_routes({(WETH.lower(), TOKEN.lower()): 1_000_000}, []), now_fn=lambda: 1_000)
    assert quote["minBuyAmount"] == "970000"
    lowered = Q.with_lower_min_out(quote, 940_000)
    old, new = quote["transaction"]["data"], lowered["transaction"]["data"]
    assert lowered["minBuyAmount"] == "940000" and int(new[74:138], 16) == 940_000
    assert old[:74] == new[:74] and old[138:] == new[138:]          # amountIn, path, to, deadline
    assert lowered["buyAmount"] == quote["buyAmount"] and quote["minBuyAmount"] == "970000"
    assert Q.with_lower_min_out(quote, 970_000) is None                # not lower
    assert Q.with_lower_min_out(quote, 0) is None                      # not positive
    assert Q.with_lower_min_out({k: v for k, v in quote.items() if k != "_fot_exit"}, 1) is None
