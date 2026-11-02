"""The PCS V3 -> V2 mixed hop as a live ladder rung on chain 56 (spec 2026-09-25, 7ef40f05).

螃蟹 0x7bc0…7777 (ref b849bb8a): its only market is a PCS V2 pair against oANTHROPIC, whose only
market is a PCS V3 pool against USDT. Every single-venue rung priced a dust pool (v3 -86%, v2
-99.7%, kyber -41.9%); the mixed route read -2.28% on chain. These tests pin the rung's contract:
the measured round trip, the whole-balance exit proof before any quote, the SmartRouter as the only
spender, the exit on the stored route, and the switch.
"""
import json

import pytest
from eth_abi import decode

from sniper.execution import evm_mixed_hop_router as R

WBNB = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"
USDT = "0x55d398326f99059ff775485246999027b3197955"
OANTH = "0x24b518b332543a4e1c3649d063dab199d103760a"
COIN = "0x7bc023e0693589817ed258cd1507b66b1e207777"
TAKER = "0x" + "dd" * 20
ROUTER = R.SMART_ROUTERS["56"]
CLIP = 10 ** 17


class _Cfg:
    MEMECOIN_ENTRY_SLIPPAGE_PCT = 3.0
    MEMECOIN_EXIT_SLIPPAGE_LADDER = [3.0, 10.0, 25.0]
    MEMECOIN_EVM_ROUNDTRIP_TAX_ALLOWANCE_PCT = 4.0
    MEMECOIN_EVM_MAX_TAX_PCT = 15.0

    @staticmethod
    def memecoin_evm_v2_exit_config(chain):
        return {"connectors": (WBNB, USDT)}


def _v3(chain, sell, buy, amount, taker, http=None, slippage_bps=None):
    """0.1 WBNB -> 0.0721 oANTHROPIC through USDT, and back at 0.98x."""
    if (sell, buy) == (WBNB, OANTH):
        out, tokens, fees = amount * 721 // 1000, [WBNB, USDT, OANTH], [100, 2500]
    elif (sell, buy) == (OANTH, WBNB):
        out, tokens, fees = amount * 1000 // 721 * 98 // 100, [OANTH, USDT, WBNB], [2500, 100]
    else:
        return None
    return {"buyAmount": str(out), "minBuyAmount": str(out * 97 // 100),
            "route": {"protocol": "pancake_v3", "tokens": tokens, "fees": fees}}


def _v2(chain, sell, buy, amount, taker, http=None, slippage_bps=None, tax_buffer_bps=None,
        connector_tokens=None, pair_tokens_fn=None):
    rate = 5_000_000 if (sell, buy) == (OANTH, COIN) else None
    if (sell, buy) == (COIN, OANTH):
        out = amount // 5_000_000
    elif rate:
        out = amount * rate
    else:
        return None
    return {"buyAmount": str(out), "minBuyAmount": str(out * 97 // 100),
            "route": {"protocol": "uniswap_v2", "tokens": [sell, buy]}}


def _pairs(chain, coin):
    return [OANTH] if coin == COIN else [WBNB]


def _entry(**kw):
    calls = {}
    verdict = (kw.pop("exit_ok", True), kw.pop("exit_reason", "exit_simulated"))
    exit_back = kw.pop("exit_back", CLIP * 98 // 100)

    def exit_sim(**call):
        calls.update(call)
        if verdict[0] and call.get("result_out") is not None:
            call["result_out"]["amount_out_atomic"] = (
                exit_back(call) if callable(exit_back) else exit_back)
        return verdict
    args = dict(rpc_url="http://rpc.invalid", fund_overrides_fn=lambda *a: {"stand-in": 1},
                v3_quote_fn=_v3, v2_quote_fn=_v2, pair_tokens_fn=_pairs, cfg=_Cfg,
                exit_sim_fn=exit_sim, now_fn=lambda: 1_790_000_000)
    args.update(kw)
    q, why = R.mixed_entry_quote("56", WBNB, COIN, CLIP, TAKER, **args)
    return q, why, calls


def test_the_rung_quotes_the_real_route_and_measures_its_round_trip():
    q, why, calls = _entry()
    assert why is None
    route = q["route"]
    assert route["protocol"] == "pcs_mixed_v3_v2" and route["base_token"] == OANTH
    assert route["v3_tokens"] == [WBNB, USDT, OANTH] and route["v3_fees"] == [100, 2500]
    assert route["round_trip_pct"] == pytest.approx(-2.0)
    assert route["round_trip_back_atomic"] == str(CLIP * 98 // 100)
    assert q["transaction"]["to"] == ROUTER
    assert q["issues"]["allowance"]["spender"] == ROUTER   # the ONLY spender this rung names
    # the exit proof ran at the WHOLE bought amount, on the reverse path
    assert calls["amount_in"] == int(q["buyAmount"])
    assert calls["v3_tokens"] == [OANTH, USDT, WBNB]


def test_entry_has_one_route_wide_slippage_floor_in_the_signed_multicall():
    q, why, _ = _entry()
    assert why is None
    floor = int(q["buyAmount"]) * 9700 // 10000
    assert int(q["minBuyAmount"]) == floor
    body = bytes.fromhex(q["transaction"]["data"][10:])
    _deadline, legs = decode(["uint256", "bytes[]"], body)
    assert int.from_bytes(legs[1][36:68], "big") == floor


def test_intermediate_tax_is_not_assumed_zero_in_forward_floor():
    seen = []

    def v2(chain, sell, buy, amount, taker, **kw):
        if (sell, buy) == (OANTH, COIN):
            seen.append(kw["tax_buffer_bps"])
        return _v2(chain, sell, buy, amount, taker, **kw)

    q, why, _ = _entry(v2_quote_fn=v2)
    assert why is None and q is not None
    assert seen == [400]


def test_reverse_uses_actual_simulation_proceeds_without_another_quote():
    seen = []

    def v3(chain, sell, buy, amount, taker, **kw):
        if (sell, buy) == (OANTH, WBNB):
            seen.append(amount)
        return _v3(chain, sell, buy, amount, taker)

    q, why, _ = _entry(v3_quote_fn=v3)
    assert why is None and q is not None
    assert seen == []
    assert q["route"]["round_trip_back_atomic"] == str(CLIP * 98 // 100)


def test_intermediate_with_smaller_buy_wins_when_its_round_trip_is_better():
    other = "0x" + "ab" * 20

    def v3(_chain, sell, buy, amount, _taker, **_kw):
        if sell == WBNB and buy in (OANTH, other):
            out = 1_000
        elif buy == WBNB and sell in (OANTH, other):
            out = 50_000 if sell == OANTH else 95_000
        else:
            return None
        return {"buyAmount": str(out), "minBuyAmount": str(out * 97 // 100),
                "route": {"protocol": "pancake_v3", "tokens": [sell, buy], "fees": [100]}}

    def v2(_chain, sell, buy, _amount, _taker, **_kw):
        if buy == COIN:
            out = 110 if sell == OANTH else 100
        elif sell == COIN:
            out = 500 if buy == OANTH else 1_000
        else:
            return None
        return {"buyAmount": str(out), "minBuyAmount": str(out * 97 // 100),
                "route": {"tokens": [sell, buy]}}

    quote, reason, _ = _entry(v3_quote_fn=v3, v2_quote_fn=v2,
                              pair_tokens_fn=lambda *_: [OANTH, other],
                              exit_back=lambda call: 50_000 if call["base_token"] == OANTH else 95_000)
    assert reason is None
    assert quote["route"]["base_token"] == other
    assert quote["buyAmount"] == "100"
    def only_first_base(**kw):
        if kw["base_token"] == OANTH:
            kw["result_out"]["amount_out_atomic"] = 50_000
            return True, "exit_simulated"
        return False, "exit_sim_reverted:STF"

    fallback, reason, _ = _entry(
        v3_quote_fn=v3, v2_quote_fn=v2,
        pair_tokens_fn=lambda *_: [OANTH, other],
        exit_sim_fn=only_first_base)
    assert reason is None and fallback["route"]["base_token"] == OANTH


def test_no_exit_proof_means_no_quote():
    q, why, _ = _entry(exit_ok=False, exit_reason="exit_sim_reverted:STF")
    assert q is None and why == "exit_unproven:exit_sim_reverted:STF"
    q, why, _ = _entry(exit_ok=False, exit_reason="exit_sim_unknown")
    assert q is None and why == "exit_unproven:exit_sim_unknown"     # transport is not "may sell"


def test_without_the_override_builder_the_default_proof_refuses():
    q, why = R.mixed_entry_quote("56", WBNB, COIN, CLIP, TAKER, rpc_url="http://rpc.invalid",
                                 fund_overrides_fn=None, v3_quote_fn=_v3, v2_quote_fn=_v2,
                                 pair_tokens_fn=_pairs, cfg=_Cfg)
    assert q is None and why == "exit_unproven:exit_overrides_unsupported"


def test_a_connector_paired_coin_costs_nothing_but_the_lookup():
    asked = []

    def v3(*a, **k):
        asked.append(a)
        return None
    q, why = R.mixed_entry_quote("56", WBNB, "0x" + "ee" * 20, CLIP, TAKER,
                                 rpc_url="http://rpc.invalid", fund_overrides_fn=None,
                                 v3_quote_fn=v3, v2_quote_fn=_v2, pair_tokens_fn=_pairs, cfg=_Cfg)
    assert q is None and why == "no_pair_token_discovered" and asked == []


def test_the_v2_leg_must_be_the_one_direct_pair():
    def v2_via_connector(chain, sell, buy, amount, taker, **k):
        return {"buyAmount": "1", "minBuyAmount": "1",
                "route": {"tokens": [sell, WBNB, buy]}}
    q, why, _ = _entry(v2_quote_fn=v2_via_connector)
    assert q is None and why == "no_v3_bridge_or_v2_leg"


def test_v3_quote_must_end_at_the_discovered_intermediate_contract():
    other = "0x" + "ab" * 20

    def wrong_first(_chain, sell, buy, amount, _taker, **_kw):
        q = _v3(_chain, sell, buy if buy == WBNB else OANTH, amount, _taker)
        if q is not None and buy == other:
            q["route"]["tokens"] = [WBNB, USDT, OANTH]
            q["buyAmount"] = str(CLIP * 10)
        return q

    def v2(_chain, sell, buy, amount, _taker, **_kw):
        if {sell, buy} != {other, COIN}:
            return _v2(_chain, sell, buy, amount, _taker)
        out = amount * 10 if sell == other else amount // 10
        return {"buyAmount": str(out), "minBuyAmount": str(out * 97 // 100),
                "route": {"tokens": [sell, buy]}}

    q, why, _ = _entry(v3_quote_fn=wrong_first,
                       v2_quote_fn=v2, pair_tokens_fn=lambda *_: [other, OANTH])
    assert why is None and q["route"]["base_token"] == OANTH


def test_malformed_v3_fee_skips_one_candidate_without_hiding_valid_route():
    other = "0x" + "ab" * 20

    def bad_fee(_chain, sell, buy, amount, _taker, **_kw):
        q = _v3(_chain, sell, buy if buy == WBNB else OANTH, amount, _taker)
        if q is not None and buy == other:
            q["route"]["fees"] = ["invalid", 100]
        return q

    q, why, _ = _entry(v3_quote_fn=bad_fee,
                       pair_tokens_fn=lambda *_: [other, OANTH])
    assert why is None and q["route"]["base_token"] == OANTH


def test_discovery_symbol_is_not_an_intermediate_contract():
    seen = []

    def v3(chain, sell, buy, amount, taker, **kw):
        seen.append(buy)
        return _v3(chain, sell, buy, amount, taker)

    q, why, _ = _entry(v3_quote_fn=v3,
                       pair_tokens_fn=lambda *_: ["STOCK", OANTH])
    assert why is None and q["route"]["base_token"] == OANTH
    assert "stock" not in seen


def test_the_exit_quote_sells_through_the_stored_base_and_the_router_balance():
    q, why = R.mixed_exit_quote("56", COIN, WBNB, 10 ** 22, TAKER,
                                route={"protocol": "pcs_mixed_v3_v2", "base_token": OANTH},
                                v3_quote_fn=_v3, v2_quote_fn=_v2, pair_tokens_fn=_pairs,
                                cfg=_Cfg, now_fn=lambda: 1_790_000_000)
    assert why is None and q["transaction"]["to"] == ROUTER
    assert q["issues"]["allowance"]["spender"] == ROUTER
    body = bytes.fromhex(q["transaction"]["data"][10:])
    _deadline, legs = decode(["uint256", "bytes[]"], body)
    (_path, recipient, amount_in, _min), = decode(["(bytes,address,uint256,uint256)"], legs[1][4:])
    assert amount_in == R.CONTRACT_BALANCE and recipient.lower() == TAKER
    assert int(q["minBuyAmount"]) == int(q["buyAmount"]) * 7500 // 10000
    assert _min == int(q["minBuyAmount"])


# --- the rung inside the real ladder ---------------------------------------------------------------

def _ladder(monkeypatch, *, enabled=True, chain="56"):
    from sniper.settings import memecoin as cfg
    from sniper.execution.evm_real_seams import venue_quote_with_v2_fallback
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_MIXED_HOP_EXECUTION_ENABLED", enabled)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ENTRY_MEASURED_LADDER", True)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V2_ENTRY_ENABLED", False)
    rungs = []

    def dust_v3(sell, buy, amount, slippage_bps=None):
        # the dust pool: in, and back out at -86%
        return {"buyAmount": str(amount * 7 // 50 if sell == COIN else amount),
                "sellAmount": str(amount), "transaction": {"to": "0xv3router"}, "route": {}}
    fn = venue_quote_with_v2_fallback(dust_v3, chain=chain, quote_token=WBNB, taker=TAKER,
                                      acct=object(), rpc_url="https://rpc", on_rung=rungs.append)
    return fn, rungs


def test_the_ladder_selects_the_mixed_route_over_a_dust_pool(monkeypatch):
    seen = {}

    def fake_entry(chain, sell, buy, amount, taker, **kw):
        seen["fund_overrides_fn"] = kw.get("fund_overrides_fn")
        return {"buyAmount": "360727", "sellAmount": str(amount),
                "transaction": {"to": ROUTER},
                "issues": {"allowance": {"spender": ROUTER}},
                "route": {"protocol": "pcs_mixed_v3_v2", "round_trip_pct": -2.28}}, None
    monkeypatch.setattr(R, "mixed_entry_quote", fake_entry)
    fn, rungs = _ladder(monkeypatch)
    marker = object()
    fn.fund_overrides_fn = marker
    q = fn(WBNB, COIN, CLIP)
    assert q is not None and q["route"]["protocol"] == "pcs_mixed_v3_v2"
    assert seen["fund_overrides_fn"] is marker          # the binding reaches the proof
    names = {r["rung"]: r for r in rungs}
    assert names["mixed_v3_v2"]["hit"] is True


def test_an_over_budget_mixed_route_is_refused_like_any_rung(monkeypatch):
    monkeypatch.setattr(R, "mixed_entry_quote", lambda *a, **k: ({
        "buyAmount": "1", "sellAmount": "1", "transaction": {"to": ROUTER},
        "route": {"protocol": "pcs_mixed_v3_v2", "round_trip_pct": -60.0}}, None))
    fn, rungs = _ladder(monkeypatch)
    assert fn(WBNB, COIN, CLIP) is None
    rec = {r["rung"]: r for r in rungs}["mixed_v3_v2"]
    assert rec["hit"] is False and str(rec.get("reason")).startswith("round_trip_over_budget")


@pytest.mark.parametrize("enabled,chain,field", [(False, "56", "skipped"),
                                                 (True, "8453", "unavailable")])
def test_the_switch_and_the_chain_gate(monkeypatch, enabled, chain, field):
    monkeypatch.setattr(R, "mixed_entry_quote",
                        lambda *a, **k: pytest.fail("the rung ran while it was switched off"))
    fn, rungs = _ladder(monkeypatch, enabled=enabled, chain=chain)
    fn(WBNB, COIN, CLIP)
    rec = {r["rung"]: r for r in rungs}["mixed_v3_v2"]
    assert rec[field] is True


# --- the exit of a held mixed position ---------------------------------------------------------------

def _executor(monkeypatch):
    from eth_account import Account
    from sniper.execution import evm_real_seams as S, evm_wallet, evm_dex_quote, evm_v2_exit_quote
    monkeypatch.setattr(evm_wallet, "erc20_balance_raw", lambda *_a, **_k: 10 ** 22)
    monkeypatch.setattr(S, "quote_usd_price", lambda *_a, **_k: 600.0)
    monkeypatch.setattr(evm_dex_quote, "dex_v3_quote", lambda *_a, **_k: {
        "buyAmount": str(10 ** 15), "transaction": {"to": "0xv3dust"}})
    monkeypatch.setattr(evm_v2_exit_quote, "dex_v2_exit_quote", lambda *_a, **_k: None)
    return S.build_real_evm_executor(
        acct=Account.create(), chain_id="56", rpc_url="https://rpc",
        http=lambda *_a, **_k: (200, {}, None), usdc_fn=lambda: 0.0, token_fn=lambda _m: 0.0)


MIXED_POSITION = {"mint": COIN, "entry_notional": 60.0,
                  "evm_route_json": json.dumps({"protocol": "pcs_mixed_v3_v2", "base_token": OANTH})}


def test_a_held_mixed_position_exits_on_the_mixed_route_even_with_the_switch_off(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_MIXED_HOP_EXECUTION_ENABLED", False)
    seen = {}

    def fake_exit(chain, coin, quote, amount, taker, **kw):
        seen["route"] = kw.get("route")
        return {"buyAmount": str(97 * 10 ** 15), "transaction": {"to": ROUTER}}, None
    monkeypatch.setattr(R, "mixed_exit_quote", fake_exit)
    preview = _executor(monkeypatch)["preview_exit_fn"](MIXED_POSITION)
    assert preview.route_id == ROUTER
    assert seen["route"]["base_token"] == OANTH


def test_if_the_mixed_route_disappears_the_exit_falls_through_to_the_ladder(monkeypatch):
    monkeypatch.setattr(R, "mixed_exit_quote", lambda *a, **k: (None, "no_mixed_exit_route"))
    preview = _executor(monkeypatch)["preview_exit_fn"](MIXED_POSITION)
    assert preview.route_id == "0xv3dust"


def test_the_chain_context_binds_the_override_builder_onto_the_ladder(monkeypatch, tmp_path):
    from sniper.execution.evm_wallet import generate_keypair_file
    from sniper.services import memecoin_evm_leg as leg
    from sniper.settings import memecoin as cfg
    kpf = str(tmp_path / "evm.json")
    generate_keypair_file(kpf)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_KEYPAIR", kpf)
    monkeypatch.setenv("MEMECOIN_EVM_RPC_URL_56", "https://bsc-rpc")
    monkeypatch.setenv("MEMECOIN_EVM_EXEC_VENUE_56", "direct")
    monkeypatch.delenv("MEMECOIN_EVM_SUBMIT_MODE", raising=False)

    def http(_method, _url, **kwargs):
        request = json.loads(kwargs["body"])
        if request["method"] == "eth_chainId":
            return 200, {"result": hex(56)}, None
        return 200, {"result": "0x"}, None

    rc = leg.build_real_evm_context("56", http=http)
    assert rc is not None
    assert callable(getattr(rc["venue_quote_fn"], "fund_overrides_fn", None))


def test_both_mixed_route_transactions_carry_their_own_gas_limit():
    """MEMECOIN_EVM_DEFAULT_ROUTE_GAS (900,000): 382,080 of the old 500,000 was the tightest fill."""
    q, why, _ = _entry()
    assert why is None and q["transaction"]["gas"] == "900000"
    q, why = R.mixed_exit_quote("56", COIN, WBNB, 10 ** 22, TAKER,
                                route={"protocol": "pcs_mixed_v3_v2", "base_token": OANTH},
                                v3_quote_fn=_v3, v2_quote_fn=_v2, pair_tokens_fn=_pairs,
                                cfg=_Cfg, now_fn=lambda: 1_790_000_000)
    assert why is None and q["transaction"]["gas"] == "900000"
