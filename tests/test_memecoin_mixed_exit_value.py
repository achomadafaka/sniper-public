"""Mixed held exits value the chosen route without changing its signed floors."""
import copy
import json
import sqlite3

import pytest
from eth_abi import decode
from eth_account import Account

from sniper.execution import evm_dex_quote as v3, evm_mixed_hop_router as mixed
from sniper.execution import evm_real_seams as seams, evm_v2_exit_quote as v2, evm_wallet
from sniper.settings import memecoin as cfg

COIN = "0x7d6d4b711fb7c4aad9f162d93e8e51034ab5ffff"
BASE = "0x205812cdbed920aff76c6580abd681a46d11efc7"
USDT = "0x55d398326f99059ff775485246999027b3197955"
WBNB = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"
TAKER = "0x" + "ab" * 20
V2_ROUTER = "0x10ed43c718714eb63d5aa57b78b54704e256024e"
BAG = 1_000_000
PATH = [BASE, USDT, WBNB]
FEES = [100, 100]


def _amounts_result(amounts):
    return "0x" + format(32, "064x") + format(len(amounts), "064x") + "".join(
        format(x, "064x") for x in amounts)


def _v2_out(amount):
    # Constant-product pool, non-negligible impact. Independent model of the RPC answer.
    return amount * 9975 * 10**18 // (10_000_000 * 10_000 + amount * 9975)


def _v3_out(amount):
    # A nonlinear second leg; rescaling final output by 96/85 gives a wrong value.
    return amount * 2 * 10**18 // (10**18 + amount)


def _deps(monkeypatch, *, v2_failure=False, v3_failure=False):
    calls = []
    real_v2 = v2.dex_v2_exit_quote
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_MAX_TAX_PCT", 15.0)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_EXIT_VALUE_TAX_PCT", 4.0)
    monkeypatch.setattr(cfg, "MEMECOIN_EXIT_SLIPPAGE_LADDER", [10.0, 25.0])
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_FLAP_QUOTE_EXIT", False)

    def call_v2(to, data):
        assert to.lower() == V2_ROUTER
        assert data.startswith("0x" + v2.GET_AMOUNTS_OUT)
        amount = int(data[10:74], 16)
        calls.append(("v2", amount))
        if v2_failure and amount == 960_000:
            raise v2.V2ReadUnavailable("rate limit")
        return _amounts_result([amount, _v2_out(amount)])

    def quote_v2(chain, sell, buy, amount, taker, **kw):
        return real_v2(
            chain, sell, buy, amount, taker, cfg_router=V2_ROUTER,
            slippage_bps=kw["slippage_bps"], tax_buffer_bps=kw.get("tax_buffer_bps"),
            connector_tokens=(), pair_tokens_fn=lambda *_: (), call_fn=call_v2,
            now_fn=lambda: 1_000)

    def quote_v3(chain, sell, buy, amount, taker, **kw):
        calls.append(("v3", amount, copy.deepcopy(kw)))
        if v3_failure and kw.get("paths_only"):
            return None
        out = _v3_out(amount)
        return {"sellAmount": str(amount), "buyAmount": str(out),
                "minBuyAmount": str(out * 9000 // 10000),
                "route": {"protocol": "pancake_v3", "tokens": PATH[:], "fees": FEES[:]}}

    monkeypatch.setattr(v2, "_make_call_fn", lambda *_: call_v2)
    return calls, quote_v2, quote_v3


def _quote(monkeypatch, **failures):
    calls, q2, q3 = _deps(monkeypatch, **failures)
    quote, why = mixed.mixed_exit_quote(
        "56", COIN, WBNB, BAG, TAKER,
        route={"protocol": mixed.PROTOCOL, "base_token": BASE},
        v2_quote_fn=q2, v3_quote_fn=q3, slippage_bps=1000,
        now_fn=lambda: 1_000)
    assert why is None
    return quote, calls, q3


def _preview(monkeypatch, **failures):
    calls, q2, q3 = _deps(monkeypatch, **failures)
    monkeypatch.setattr(v2, "dex_v2_exit_quote", q2)
    # q2 retains the real builder; only external provider answers are modeled.
    monkeypatch.setattr(v3, "dex_v3_quote", q3)
    monkeypatch.setattr(evm_wallet, "erc20_balance_raw", lambda *_a, **_k: BAG)
    monkeypatch.setattr(seams, "quote_usd_price", lambda *_a, **_k: 750.08)
    ex = seams.build_real_evm_executor(
        acct=Account.create(), chain_id="56", rpc_url="https://rpc.invalid",
        http=lambda *_a, **_k: (200, {}, None), usdc_fn=lambda: 0.0,
        token_fn=lambda _m: 0.0)
    position = {"mint": COIN, "entry_notional": 123.82,
                "evm_route_json": json.dumps({"protocol": mixed.PROTOCOL, "base_token": BASE})}
    return ex["preview_exit_fn"](position), calls


def test_real_preview_prices_whole_bag_allowance_in_final_quote_units(monkeypatch):
    result, calls = _preview(monkeypatch)
    assert result.failure is None
    expected_atomic = _v3_out(_v2_out(960_000))
    cut_atomic = _v3_out(_v2_out(850_000))
    assert result.expected_usdc == pytest.approx(expected_atomic / 10**18 * 750.08)
    signed = result.preview.quote
    assert int(signed["buyAmount"]) == expected_atomic
    assert signed["_cut_buyAmount"] == str(cut_atomic)
    assert signed["_value_basis"] == "whole_bag_less_allowance"
    assert "_fot_exit" not in signed  # V2 calldata patcher must never patch this multicall.
    assert int(signed["minBuyAmount"]) == cut_atomic * 9000 // 10000
    _, legs = decode(["uint256", "bytes[]"], bytes.fromhex(signed["transaction"]["data"][10:]))
    amount, minimum, tokens, recipient = decode(
        ["uint256", "uint256", "address[]", "address"], legs[0][4:])
    assert amount == BAG and minimum == _v2_out(850_000) * 9000 // 10000
    assert list(tokens) == [COIN, BASE] and recipient == mixed.SMART_ROUTERS["56"]
    (encoded_path, recipient, amount, minimum), = decode(
        ["(bytes,address,uint256,uint256)"], legs[1][4:])
    assert amount == 0 and minimum == int(signed["minBuyAmount"])
    assert recipient == signed["_mixed_exit"]["taker"]
    assert encoded_path.hex() == BASE[2:] + "000064" + USDT[2:] + "000064" + WBNB[2:]
    assert result.quote_evidence["value_basis"] == "whole_bag_less_allowance"
    assert result.quote_evidence["expected_output_atomic"] == str(expected_atomic)
    assert result.quote_evidence["quote_asset"].lower() == WBNB
    assert result.quote_evidence["quote_asset_decimals"] == 18
    assert [c[0] for c in calls] == ["v2", "v3", "v2", "v3"]
    assert calls[-1][2]["paths_only"] is True
    assert calls[-1][2]["extra_paths"] == [(PATH, FEES)]
    assert expected_atomic != cut_atomic * 9600 // 8500


def test_mixed_value_reprices_only_selected_path_without_mutating_quote(monkeypatch):
    quote, calls, q3 = _quote(monkeypatch)
    before = copy.deepcopy(quote)
    value = mixed.exit_value_amount(quote, http=None, taker=TAKER, v3_quote_fn=q3)
    assert value == _v3_out(_v2_out(960_000))
    assert quote == before
    assert calls[-1][2]["extra_paths"] == [(PATH, FEES)]


@pytest.mark.parametrize("allowance", [15.0, 16.0])
def test_rollback_at_max_tax_keeps_preview_cut_without_extra_reads(monkeypatch, allowance):
    quote, calls, q3 = _quote(monkeypatch)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_EXIT_VALUE_TAX_PCT", allowance)
    before = len(calls)
    assert mixed.exit_value_amount(quote, taker=TAKER, v3_quote_fn=q3) is None
    assert len(calls) == before


@pytest.mark.parametrize("failures,count", [({"v2_failure": True}, 3), ({"v3_failure": True}, 4)])
def test_failed_value_read_preserves_usable_conservative_preview(monkeypatch, failures, count):
    result, calls = _preview(monkeypatch, **failures)
    assert result.failure is None
    assert result.expected_usdc == pytest.approx(_v3_out(_v2_out(850_000)) / 10**18 * 750.08)
    assert result.preview.quote["_value_basis"] == "max_tax_cut"
    assert len(calls) == count


@pytest.mark.parametrize("change", [
    lambda q: q.pop("_mixed_exit"),
    lambda q: q.update(_mixed_exit=None),
    lambda q: q["_mixed_exit"].update(chain="8453"),
    lambda q: q["_mixed_exit"].update(coin=USDT),
    lambda q: q["_mixed_exit"].update(quote_token=USDT),
    lambda q: q["_mixed_exit"]["v2_quote"].update(sellAmount=str(BAG + 1)),
    lambda q: q["_mixed_exit"]["v2_quote"]["_fot_exit"].update(path=[COIN, USDT]),
    lambda q: q["_mixed_exit"]["v2_quote"]["_fot_exit"].update(router=TAKER),
    lambda q: q["_mixed_exit"]["v2_quote"]["_fot_exit"].update(chain="8453"),
    lambda q: q["route"].update(protocol="uniswap_v2"),
    lambda q: q["route"].update(router=TAKER),
    lambda q: q["transaction"].update(to=TAKER),
    lambda q: q["route"].update(base_token=USDT),
    lambda q: q["route"].update(exit_v3_tokens=[USDT, WBNB]),
    lambda q: q["route"].update(exit_v3_tokens=[BASE, USDT]),
    lambda q: q["route"].update(exit_v3_fees=[100]),
    lambda q: q["route"].update(exit_v3_fees=[True, 100]),
    lambda q: q["route"].update(exit_v3_fees=[1.5, 100]),
    lambda q: q["route"].update(exit_v3_fees=[0, 100]),
    lambda q: q.update(sellAmount="nan"),
    lambda q: q.update(sellAmount=True),
    lambda q: q.update(sellAmount=str(2**256)),
])
def test_wrong_or_malformed_binding_is_unknown_before_value_reads(monkeypatch, change):
    quote, calls, q3 = _quote(monkeypatch)
    change(quote)
    before = len(calls)
    assert mixed.exit_value_amount(quote, taker=TAKER, v3_quote_fn=q3) is None
    assert len(calls) == before


@pytest.mark.parametrize("change", [
    lambda q: None, lambda q: {},
    lambda q: dict(q, buyAmount="0"), lambda q: dict(q, buyAmount="nan"),
    lambda q: dict(q, buyAmount=True), lambda q: dict(q, buyAmount=str(2**256)),
    lambda q: dict(q, sellAmount="1"),
    lambda q: dict(q, route={"protocol": "pancake_v3", "tokens": [BASE, WBNB], "fees": [100]}),
    lambda q: dict(q, route={"protocol": "pancake_v3", "tokens": PATH, "fees": [500, 100]}),
    lambda q: dict(q, route={"protocol": "uniswap_v3", "tokens": PATH, "fees": FEES}),
])
def test_unknown_or_wrong_v3_reply_cannot_be_used_as_value(monkeypatch, change):
    quote, calls, _ = _quote(monkeypatch)
    reply = change({"buyAmount": "10", "sellAmount": str(_v2_out(960_000)),
                    "route": {"protocol": "pancake_v3", "tokens": PATH, "fees": FEES}})
    assert mixed.exit_value_amount(quote, taker=TAKER, v3_quote_fn=lambda *_a, **_k: reply) is None
    assert calls[-1] == ("v2", 960_000)


@pytest.mark.parametrize("exc", [TimeoutError("timeout"), v3.V3ReadUnavailable("rate_limit"),
                                OverflowError("malformed quote")])
def test_value_dependency_exception_does_not_remove_sell_route(monkeypatch, exc):
    quote, _, _ = _quote(monkeypatch)

    def down(*_a, **_k):
        raise exc

    before = copy.deepcopy(quote)
    assert mixed.exit_value_amount(quote, taker=TAKER, v3_quote_fn=down) is None
    assert quote == before


def test_repriced_prepared_preview_signs_original_mixed_transaction_without_requote(monkeypatch):
    from sniper.execution.evm_exit_executor import execute_exit_evm
    from sniper.persistence import db_memecoin as m

    preview, calls = _preview(monkeypatch)
    expected_tx = copy.deepcopy(preview.preview.quote["transaction"])
    qty = BAG / 10**18
    signed = []
    with sqlite3.connect(":memory:") as c:
        c.row_factory = sqlite3.Row
        m.ensure_memecoin_tables(c)
        pid = m.open_position(
            c, source_user_id="u", source_handle="source", mint=COIN, ticker="4cat",
            entry_price=50.0 / qty, entry_notional=50.0, entry_fill_source="chain",
            entry_lag_s=0.0, their_size_usd=0.0, opened_at="2026-10-10T17:00:00Z",
            strategy="alpha_b", execution_mode="real", network_id="56")
        m.insert_fill(c, position_id=pid, kind="entry", price=50.0 / qty, qty=qty,
            notional=50.0, fill_source="chain", fraction=1.0,
            created_at="2026-10-10T17:00:00Z", tx_sig="entry")

        def no_requote(*_a):
            pytest.fail("matching prepared mixed quote must not add provider reads")

        result = execute_exit_evm(
            c, m.get_position(c, pid), "manual", "2026-10-10T17:01:00Z",
            prepared_preview=preview, slippage_ladder=[10], quote_fn=no_requote,
            approve_fn=lambda *_a: pytest.fail("existing allowance must not be approved again"),
            allowance_fn=lambda *_a: BAG,
            sign_fn=lambda tx: signed.append(copy.deepcopy(tx)) or ("0x00", "0xhash", 1),
            send_fn=lambda *_a: None, confirm_fn=lambda *_a: "confirmed",
            usdc_fn=lambda: 50.0, token_balance_fn=lambda _m: qty,
            token_decimals_fn=lambda _m: 18,
            receipt_proceeds_fn=lambda _h: 120.0, require_receipt_proceeds=True,
            quote_asset_usd=750.08, quote_asset_decimals=18)
        assert result["status"] == "closed"
        assert signed == [expected_tx]
        assert result["received"] == 120.0
        assert len(calls) == 4


def test_corrected_preview_reaches_existing_tp_nomination_and_exact_confirmation(monkeypatch):
    from sniper.execution.exit_preview import ExitPreview, PreviewResult
    from sniper.persistence import db_memecoin as m
    from sniper.services import memecoin_real_runner as runner

    corrected, _ = _preview(monkeypatch)
    old = PreviewResult.succeeded(ExitPreview(
        108.75302225167263, BAG, mixed.SMART_ROUTERS["56"], corrected.quoted_at))
    override = {"tp_pct": 20.0}
    qty = BAG / 10**18
    with sqlite3.connect(":memory:") as c:
        c.row_factory = sqlite3.Row
        m.ensure_memecoin_tables(c)
        pid = m.open_position(
            c, source_user_id="u", source_handle="source", mint=COIN, ticker="4cat",
            entry_price=100.0 / qty, entry_notional=100.0, entry_fill_source="chain",
            entry_lag_s=0.0, their_size_usd=0.0, opened_at="2026-10-10T17:00:00Z",
            strategy="alpha_b", execution_mode="real", network_id="56")
        m.insert_fill(c, position_id=pid, kind="entry", price=100.0 / qty, qty=qty,
            notional=100.0, fill_source="chain", fraction=1.0,
            created_at="2026-10-10T17:00:00Z", tx_sig="entry")
        pos = m.get_position(c, pid)
        assert runner.alpha_b_real_exit_reason(
            pos, old.expected_usdc / qty, "2026-10-10T17:00:01Z", override=override) != "real_tp"
        assert runner.alpha_b_real_exit_reason(
            pos, corrected.expected_usdc / qty, "2026-10-10T17:00:01Z", override=override) == "real_tp"
        assert runner.executable_exit_allowed(c, pos, "real_tp", old, override=override)[0] is False
        allowed, evidence = runner.executable_exit_allowed(
            c, pos, "real_tp", corrected, override=override)
        assert allowed is True
        assert evidence["projected_pnl_pct"] == pytest.approx(20.56475896)
        assert runner.executable_exit_allowed(c, pos, "real_tp", None, override=override)[0] is False
